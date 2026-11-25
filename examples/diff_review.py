import argparse
import hashlib
import html
import http.client
import json
import re
import sqlite3
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlencode

from pagefold.codec import canonical, unpack
from pagefold.server import make_server
from pagefold.store import Store


SCHEMA = """
CREATE TABLE cell (
    region TEXT NOT NULL,
    seq INTEGER NOT NULL,
    code TEXT,
    note TEXT,
    ratio REAL,
    payload BLOB,
    PRIMARY KEY(region, seq)
) WITHOUT ROWID;
"""

KEY = ["t", "north"], ["i", "1"]
BASE_BLOB = bytes((index * 37 + 19) % 256 for index in range(1024))
RIGHT_BLOB = b"pagefold-unambiguous-blob-review:" + bytes(
    (251 - index * 13) % 256 for index in range(1024)
)
BASE_ROW = ("north", 1, "NULL", None, 1.5, BASE_BLOB)


def review_image(variant):
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO cell VALUES (?, ?, ?, ?, ?, ?)", BASE_ROW)
        if variant == "delete_left":
            db.execute("DELETE FROM cell WHERE region='north' AND seq=1")
        elif variant == "delete_right":
            db.execute(
                "UPDATE cell SET code=?, note=?, ratio=?, payload=? "
                "WHERE region='north' AND seq=1",
                ("右支改写：文本 NULL 被替换", "右支真实 NULL 改为文本", 2.75, RIGHT_BLOB),
            )
        elif variant == "double_left":
            db.execute(
                "UPDATE cell SET note=?, ratio=? WHERE region='north' AND seq=1",
                ("左支填入原真实 NULL", 10.125),
            )
        elif variant == "double_right":
            db.execute(
                "UPDATE cell SET code=?, ratio=?, payload=? WHERE region='north' AND seq=1",
                ("RIGHT-DOUBLE", 20.25, RIGHT_BLOB),
            )
        elif variant != "base":
            raise ValueError(variant)
        db.commit()
        return db.serialize()
    finally:
        db.close()


class Http:
    def __init__(self, port):
        self.port = port

    def request(self, method, route, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, route, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, response.getheader("Location"), payload)
        connection.close()
        return result

    def json(self, method, route, value=None, status=200):
        body = None if value is None else json.dumps(value).encode("utf-8")
        headers = {} if body is None else {"Content-Type": "application/json"}
        actual, _, payload = self.request(method, route, body, headers)
        assert actual == status, f"{route}: expected {status}, got {actual}: {payload[:500]!r}"
        return json.loads(payload)

    def upload(self, name, data):
        return self.request(
            "POST", "/api/images", data,
            {"Content-Type": "application/octet-stream", "X-Image-Name": name},
        )

    def form_decision(self, plan_id, item_id, choice):
        body = urlencode({"revision": "0", "item": item_id, "choice": choice}).encode("utf-8")
        status, location, payload = self.request(
            "POST", f"/plans/{plan_id}/decisions", body,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert status == 303, f"表单裁决未重定向: {status} {payload[:300]!r}"
        assert location == f"/plans/{plan_id}", location


def extract_article(page, item_id):
    match = re.search(
        r'<article\b[^>]*data-item-id="' + re.escape(item_id) + r'"[\s\S]*?</article>',
        page,
    )
    assert match, "页面缺少冲突条目 <article>"
    return match.group(0)


def assert_page_object(page_text, item, kind, selected=None):
    article = extract_article(page_text, item["id"])
    assert f'data-conflict-kind="{kind}"' in article
    assert 'class="diff-grid"' in article and 'class="column-row"' in article
    assert "声明类型" in article and ">INTEGER<" in article and ">TEXT<" in article
    assert ">REAL<" in article and ">BLOB<" in article
    assert "文本字符串 “NULL”" in article, "未明确区分文本 NULL"
    assert "真 NULL（SQL 空值，不是文本）" in article, "未明确标记真实 NULL"
    if kind == "delete-modify":
        assert "行缺席" in article, "删改冲突未标记缺席行"
    assert "无歧义核对：HEX / SQLite 字面量 / 完整 SHA-256" in article
    assert hashlib.sha256(BASE_BLOB).hexdigest() in article
    assert BASE_BLOB.hex().upper() in article
    assert f'data-key-json="{html.escape(canonical(item["key"]), quote=True)}"' in article

    for role in ("base", "left", "right"):
        row_json = html.escape(canonical(item[role]), quote=True)
        assert row_json in article, f"页面缺少 {role} 的类型化 JSON 对象"

    if selected is not None:
        assert f'data-current-choice="{selected}"' in article
        option = re.search(r'<option value="' + selected + r'"([^>]*)>', article)
        assert option and "selected" in option.group(1), "保存后的下拉框未显示选中对象"
        assert re.search(r'<form[^>]*action="/plans/[^"]+/decisions"[^>]*>', article)
        assert re.search(r'<input type="hidden" name="item" value="' + re.escape(item["id"]) + r'">',
                         article), "页面表单绑定的 item 与冲突对象不一致"
        assert '<input type="hidden" name="revision" value="1">' in article
        card = re.search(r'<section[^>]*data-role="' + selected + r'"[^>]*>', article)
        assert card, "缺少选中角色后果卡"
        class_match = re.search(r'class="([^"]*)"', card.group(0))
        assert class_match and "is-selected" in class_match.group(1)
        row_json = re.search(r'data-row-json="([^"]+)"', card.group(0))
        assert row_json, "选中后果卡缺少 JSON 对象"
        assert json.loads(html.unescape(row_json.group(1))) == item[selected], \
            "页面选中对象的 JSON 语义与 API 不一致"
        action = outcome_action(item, selected)
        assert f'data-action="{action}"' in card.group(0)


def outcome_action(item, role):
    row, base = item[role], item["base"]
    if row is None and base is not None:
        return "delete"
    if row is None:
        return "absent"
    if row != base:
        return "write"
    return "keep-base"


def readable(row):
    if row is None:
        return "<缺席>"
    values = []
    for value in row:
        if isinstance(value, bytes):
            values.append(f"<BLOB {len(value)}B sha256={hashlib.sha256(value).hexdigest()[:16]}…>")
        else:
            values.append(repr(value))
    return "(" + ", ".join(values) + ")"


def setup_sources(client):
    images = {}
    for variant in ("base", "delete_left", "delete_right", "double_left", "double_right"):
        status, _, payload = client.upload(f"review-{variant}", review_image(variant))
        assert status == 201, f"镜像 {variant} 上传失败: {status} {payload[:300]!r}"
        images[variant] = json.loads(payload)["id"]
    return images


def verify_choice(client, images, scenario, kind, left_name, right_name, choice):
    sources = {"base": images["base"], "left": images[left_name], "right": images[right_name]}
    plan = client.json("POST", "/api/plans", sources, 201)
    assert len(plan["items"]) == 1
    item = plan["items"][0]
    assert item["key"] == [list(KEY[0]), list(KEY[1])], item["key"]

    page_status, _, page_bytes = client.request("GET", f"/plans/{plan['id']}")
    assert page_status == 200
    page_text = page_bytes.decode("utf-8")
    assert_page_object(page_text, item, kind)

    client.form_decision(plan["id"], item["id"], choice)
    chosen_plan = client.json("GET", f"/api/plans/{plan['id']}", status=200)
    assert chosen_plan["revision"] == 1 and chosen_plan["items"][0]["choice"] == choice

    _, _, chosen_page_bytes = client.request("GET", f"/plans/{plan['id']}")
    assert_page_object(chosen_page_bytes.decode("utf-8"), item, kind, selected=choice)

    rehearsal = client.json("POST", f"/api/plans/{plan['id']}/rehearse", {"revision": 1})
    assert rehearsal["status"] == "passed", rehearsal
    certificate = client.json("POST", f"/api/plans/{plan['id']}/seal", {"revision": 1})
    assert certificate["candidate_digest"] == rehearsal["candidate_digest"]

    status, _, candidate = client.request("GET", f"/api/plans/{plan['id']}/download")
    assert status == 200 and hashlib.sha256(candidate).hexdigest() == certificate["candidate_digest"]

    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(candidate)
        actual = db.execute("SELECT region,seq,code,note,ratio,payload FROM cell").fetchone()
        db.row_factory = sqlite3.Row
    finally:
        db.close()

    expected_atoms = item[choice]
    if expected_atoms is None:
        assert actual is None, f"{scenario}/{choice}: 期望行被删除，实际 {actual!r}"
    else:
        expected = tuple(unpack(atom) for atom in expected_atoms)
        assert actual == expected, f"{scenario}/{choice}: 重演值不一致\n页面/JSON={expected!r}\n数据库={actual!r}"

    action = outcome_action(item, choice)
    print(f"  {choice:<5} -> {action:<9} revision=1 changed={rehearsal['changed']} "
          f"JSON={canonical(expected_atoms)[:88]}{'…' if len(canonical(expected_atoms)) > 88 else ''}")
    print(f"         SQLite 重读: {readable(actual)}")
    return {"scenario": scenario, "choice": choice, "action": action, "actual": actual}


def run_checks():
    with tempfile.TemporaryDirectory(prefix="pagefold-diff-review-") as directory:
        server = make_server(Store(Path(directory) / "state.sqlite"), 0)
        server.RequestHandlerClass.log_message = lambda self, *_args: None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Http(server.server_port)
            images = setup_sources(client)
            print("同一复合键 cell PRIMARY KEY(region, seq) = ('north', 1)")
            print("A. 删改冲突：左支删除，右支改写")
            for choice in ("base", "left", "right"):
                verify_choice(client, images, "delete-modify", "delete-modify",
                               "delete_left", "delete_right", choice)
            print("B. 双改冲突：左右两支分别改写不同列")
            for choice in ("base", "left", "right"):
                verify_choice(client, images, "double-modify", "double-modify",
                               "double_left", "double_right", choice)
            review_asset_status, _, review_js = client.request("GET", "/assets/review.js")
            assert review_asset_status == 200 and b"data-choice-select" in review_js
            print("\n演示通过：页面 data-row-json、API 类型化 JSON、表单选择和下载库重读值全部一致。")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def serve_state(directory, port):
    directory.mkdir(parents=True, exist_ok=True)
    store = Store(directory / "diff-review.sqlite")
    server = make_server(store, port)
    server.RequestHandlerClass.log_message = lambda self, *_args: print(
        f"{self.address_string()} {self.command} {self.path}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Http(server.server_port)
    images = setup_sources(client)
    for name, left, right in (
        ("delete-modify", "delete_left", "delete_right"),
        ("double-modify", "double_left", "double_right"),
    ):
        plan = client.json("POST", "/api/plans",
                           {"base": images["base"], "left": images[left], "right": images[right]}, 201)
        print(f"{name}: http://127.0.0.1:{server.server_port}/plans/{plan['id']}")
    print(f"Pagefold review server: http://127.0.0.1:{server.server_port}/")
    try:
        thread.join()
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="演示列对齐差异页、删改/双改冲突和 JSON/重演一致性")
    parser.add_argument("--serve", action="store_true", help="登记演示镜像并保持 HTTP 服务运行")
    parser.add_argument("--state-dir", type=Path, default=Path("./.diff-review-state"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.serve:
        serve_state(args.state_dir, args.port)
    else:
        run_checks()


if __name__ == "__main__":
    main()
