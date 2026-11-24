"""Demonstrate consistent collection from a concurrently written WAL source.

Run:
    python3 -m examples.collect_wal_demo
    python3 -m examples.collect_wal_demo --dir /tmp/pagefold-collect-demo

The script starts the real Pagefold HTTP API with a server-side source allow
list.  Timing/fault observers are used only for the in-process calls so that
concurrent writes and an interrupted paged backup are deterministic.  Those
calls invoke the same collect_source() service used by POST /api/sources/<name>.
"""

import argparse
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pagefold.errors import Problem
from pagefold.server import make_server
from pagefold.sources import SourceConfig, SourcePolicy, collect_source
from pagefold.store import Store


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def request(self, method, route, payload=None, status=200):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=15) as response:
                body = response.read()
                actual = response.status
                kind = response.headers.get_content_type()
        except HTTPError as error:
            body = error.read()
            actual = error.code
            kind = error.headers.get_content_type()
        parsed = json.loads(body) if kind == "application/json" else body
        if actual != status:
            raise RuntimeError(f"{method} {route}: expected {status}, got {actual}: {body[:500]!r}")
        return parsed

    def get(self, route, status=200):
        return self.request("GET", route, status=status)

    def post(self, route, payload=None, status=201):
        return self.request("POST", route, payload if payload is not None else {}, status)


def connect_source(path, readonly=False):
    if readonly:
        return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    return sqlite3.connect(path, timeout=30)


def commit_one(path, seq, note):
    digest = hashlib.sha256(f"operations-{seq}".encode()).hexdigest()[:16]
    db = connect_source(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO pagefold_commit(seq,commit_id,note) VALUES(?,?,?)",
                   (seq, digest, note))
        db.execute("UPDATE snapshot_head SET head_seq=? WHERE id=1", (seq,))
        db.commit()
    finally:
        db.close()
    return {"seq": seq, "commit_id": digest, "note": note,
            "at": datetime.now(timezone.utc).isoformat()}


def writer(path, first, stop_before, started=None, interval=0.006):
    commits = []
    if started is not None:
        started.wait(5)
    for seq in range(first, stop_before):
        commits.append(commit_one(path, seq, "并发写入"))
        time.sleep(interval)
    return commits


def initialize_source(path):
    db = connect_source(path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE pagefold_commit(
            seq INTEGER PRIMARY KEY,
            commit_id TEXT NOT NULL UNIQUE,
            note TEXT NOT NULL
        );
        CREATE TABLE snapshot_head(
            id INTEGER PRIMARY KEY CHECK(id = 1),
            head_seq INTEGER NOT NULL
        );
        CREATE TABLE audit_padding(
            id INTEGER PRIMARY KEY,
            payload TEXT NOT NULL
        );
        INSERT INTO snapshot_head VALUES(1, 0);
        """)
        db.executemany("INSERT INTO audit_padding VALUES(?,?)",
                       [(i, f"wal-page-{i:04d}-" + "子" * 120) for i in range(900)])
        db.commit()
    finally:
        db.close()
    for seq in range(1, 4):
        commit_one(path, seq, "初始提交")


def source_metrics(path):
    db = connect_source(path, readonly=True)
    try:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {
            "integrity_check": db.execute("PRAGMA integrity_check").fetchone()[0],
            "journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0],
            "head_seq": db.execute("SELECT head_seq FROM snapshot_head WHERE id=1").fetchone()[0],
            "commit_rows": db.execute("SELECT count(*), coalesce(max(seq),0) FROM pagefold_commit").fetchone(),
            "padding_rows": db.execute("SELECT count(*) FROM audit_padding").fetchone()[0],
            "padding_length_total": db.execute("SELECT coalesce(sum(length(payload)),0) FROM audit_padding").fetchone()[0],
            "tables": tables,
        }
    finally:
        db.close()


def file_stats(path):
    result = {}
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            stat = candidate.stat()
            result[candidate.name] = {"size": stat.st_size, "inode": stat.st_ino}
    return result


def verify_stored_body(store, record):
    with store.connect() as db:
        row = db.execute("SELECT body FROM images WHERE id=?", (record["id"],)).fetchone()
    body = bytes(row["body"])
    digest = hashlib.sha256(body).hexdigest()
    collection_digest = record.get("collection", {}).get("snapshot_digest")
    return {
        "stored_bytes": len(body),
        "stored_sha256": digest,
        "matches_image_id": digest == record["id"],
        "matches_collection_digest": digest == collection_digest,
    }


def verify_http_download(client, record):
    body = client.get(f"/api/images/{record['id']}/download")
    digest = hashlib.sha256(body).hexdigest()
    return {"download_bytes": len(body), "download_sha256": digest,
            "matches_image_id": digest == record["id"]}


def commit_summary(record):
    collection = record.get("collection", {})
    commits = collection.get("included_commits", [])
    seqs = [row["seq"] for row in commits]
    return {
        "image_id": record["id"],
        "duplicate": bool(record.get("duplicate")),
        "size": record["size"],
        "row_count": record.get("row_count"),
        "catalog_signature": record["catalog"]["signature"],
        "table_rows": {table["name"]: table["rows"] for table in record["catalog"]["tables"]},
        "included_commit_seqs": seqs,
        "included_commit_range": [seqs[0], seqs[-1]] if seqs else [],
        "backup_batches": collection.get("backup", {}).get("batches"),
        "collection_id": collection.get("id"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=None, help="保留演示状态和报告的目录")
    args = parser.parse_args()
    directory = args.dir or Path(tempfile.mkdtemp(prefix="pagefold-collect-"))
    directory.mkdir(parents=True, exist_ok=True)
    source_path = directory / "operations.sqlite"
    initialize_source(source_path)

    store = Store(directory / "pagefold-state.sqlite")
    policy = SourcePolicy([
        SourceConfig(
            "wal", source_path,
            label="运维在线库",
            commit_sql="SELECT seq, commit_id, note FROM pagefold_commit ORDER BY seq",
            pages=1,
        )
    ])
    with make_server(store, 0, policy) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = Client(server.server_port)
        try:
            report = run_demo(client, store, policy, source_path, directory)
        finally:
            server.shutdown()
            thread.join(timeout=5)

    report_path = directory / "collect-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = directory / "collect-comparison.md"
    md_path.write_text(markdown_report(report), encoding="utf-8")
    print(human_report(report))
    print(f"\n完整 JSON：{report_path}")
    print(f"比较表：{md_path}")


def run_demo(client, store, policy, source_path, directory):
    report = {"directory": str(directory), "started_at": datetime.now(timezone.utc).isoformat(),
              "policy": policy.describe(), "scenarios": []}

    denied = client.post("/api/sources/collect", {"source": "/etc/passwd"}, status=403)
    report["policy_denial"] = {"selector": "/etc/passwd", "status": 403, "response": denied}

    # 1. Stable point, through the real HTTP entry.
    before = source_metrics(source_path)
    files_before = file_stats(source_path)
    steady = client.post("/api/sources/wal/collect", {"label": "稳定时点采集"}, status=201)
    after = source_metrics(source_path)
    files_after = file_stats(source_path)
    steady_again = client.post("/api/sources/collect", {"source": "wal"}, status=201)
    steady_rows = client.get(f"/api/images/{steady['id']}/rows?table=pagefold_commit")
    report["scenarios"].append({
        "scenario": "steady_http",
        "title": "无并发写入 / HTTP 采集两次",
        "registration": commit_summary(steady),
        "second_registration": commit_summary(steady_again),
        "stored_body": verify_stored_body(store, steady),
        "http_download": verify_http_download(client, steady),
        "registered_commit_rows_endpoint": steady_rows["total"],
        "source_before": before,
        "source_after": after,
        "files_before": files_before,
        "files_after": files_after,
        "source_changed": before != after,
    })

    # 2. Paged backup overlaps a writer. The observer only supplies timing.
    race_started = threading.Event()
    race_writes = {}

    def race_observer(event):
        if event["batch"] == 1:
            race_started.set()
            time.sleep(0.05)

    def run_race_writer():
        race_writes["commits"] = writer(
            source_path, 4, 10, started=race_started, interval=0.004)

    race_thread = Thread(target=run_race_writer, daemon=True)
    race_thread.start()
    race_record = collect_source(store, policy, "wal",
                                 label="并发写入中的一致时点", observer=race_observer)
    race_thread.join(timeout=10)
    # Commits after the snapshot make the excluded suffix explicit.
    commit_one(source_path, 10, "采集返回后的提交")
    commit_one(source_path, 11, "采集返回后的提交")
    race_metrics = source_metrics(source_path)
    race_collection = client.get(f"/api/collections/{race_record['collection']['id']}")
    race_rows = client.get(f"/api/images/{race_record['id']}/rows?table=snapshot_head")
    head_atom = race_rows["rows"][0]["values"][1]  # columns: id, head_seq
    last_included_seq = (race_record["collection"]["included_commits"][-1]["seq"]
                          if race_record["collection"]["included_commits"] else None)
    report["scenarios"].append({
        "scenario": "concurrent_writes",
        "title": "WAL 持续写入时分页备份",
        "registration": commit_summary(race_record),
        "stored_body": verify_stored_body(store, race_record),
        "http_download": verify_http_download(client, race_record),
        "persisted_collection_digest": race_collection["snapshot_digest"],
        "snapshot_head_value": head_atom,
        "head_matches_last_included_commit": head_atom == ["i", str(last_included_seq)],
        "source_final": race_metrics,
        "writer_commits_during_backup": race_writes.get("commits", []),
        "excluded_commit_seqs": list(range(
            race_record["collection"]["included_commits"][-1]["seq"] + 1,
            race_metrics["head_seq"] + 1)) if race_record["collection"]["included_commits"] else [],
    })

    # 3. Interrupt a paged read while writes are arriving.
    images_before_failure = client.get("/api/images")
    collections_before_failure = client.get("/api/collections")
    interrupt_started = threading.Event()
    interrupt_writes = {}

    def interrupt_observer(event):
        if event["batch"] == 1:
            interrupt_started.set()
        elif event["batch"] == 2:
            time.sleep(0.04)
        elif event["batch"] == 4:
            raise Problem("source_interrupted", "演示注入：读源在第 4 批后中断", 502)

    def run_interrupt_writer():
        interrupt_writes["commits"] = writer(
            source_path, 12, 21, started=interrupt_started, interval=0.005)

    interrupt_thread = Thread(target=run_interrupt_writer, daemon=True)
    interrupt_thread.start()
    failure = None
    try:
        collect_source(store, policy, "wal", observer=interrupt_observer)
    except Problem as exc:
        failure = {"code": exc.code, "status": exc.status, "detail": exc.detail}
    interrupt_thread.join(timeout=10)
    images_after_failure = client.get("/api/images")
    collections_after_failure = client.get("/api/collections")
    failure_metrics = source_metrics(source_path)
    recovered = client.post("/api/sources/wal/collect", {"label": "中断恢复后采集"}, status=201)
    recovered_summary = commit_summary(recovered)
    report["scenarios"].append({
        "scenario": "interrupted_read",
        "title": "并发写入期间读源中断",
        "failure": failure,
        "registrations_before_failure": len(images_before_failure),
        "registrations_after_failure": len(images_after_failure),
        "collections_before_failure": len(collections_before_failure),
        "collections_after_failure": len(collections_after_failure),
        "failed_attempt_created_registration": len(images_before_failure) != len(images_after_failure),
        "writer_commits_during_failure": interrupt_writes.get("commits", []),
        "source_after_interruption": failure_metrics,
        "recovery_registration": recovered_summary,
        "recovery_body": verify_stored_body(store, recovered),
        "recovery_download": verify_http_download(client, recovered),
    })

    report["final_images"] = client.get("/api/images")
    report["final_collections"] = client.get("/api/collections")
    verify_report(report)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def verify_report(report):
    def expect(condition, message):
        if not condition:
            raise RuntimeError(message)

    expect(report["policy_denial"]["response"]["code"] == "source_forbidden",
           "服务端路径白名单未生效")
    steady, race, interrupted = report["scenarios"]
    expect(steady["scenario"] == "steady_http", "演示场景顺序错误")
    expect(steady["registration"]["image_id"] == steady["second_registration"]["image_id"],
           "相同时点未按摘要复用")
    expect(steady["second_registration"]["duplicate"] is True, "重复采集未标记 duplicate")
    expect(all(steady["stored_body"][key] for key in
               ("matches_image_id", "matches_collection_digest")), "稳定采集摘要不一致")
    expect(steady["http_download"]["matches_image_id"], "HTTP 下载摘要与登记 ID 不一致")
    expect(steady["source_changed"] is False, "无并发时采集改动了原库")
    expect(race["head_matches_last_included_commit"] is True,
           "结构目录/head 行与镜像内最后提交不是同一点")
    expect(race["stored_body"]["matches_image_id"] and
           race["stored_body"]["matches_collection_digest"], "并发采集摘要不一致")
    expect(race["http_download"]["matches_image_id"], "并发采集 HTTP 下载摘要不一致")
    expect(race["persisted_collection_digest"] == race["registration"]["image_id"],
           "持久化采集凭证摘要不一致")
    expect({10, 11}.issubset(set(race["excluded_commit_seqs"])),
           "采集后提交未被识别为排除后缀")
    expect(interrupted["failure"] and interrupted["failure"]["code"] == "source_interrupted",
           "未观察到读源中断")
    expect(interrupted["failed_attempt_created_registration"] is False,
           "中断尝试产生了登记")
    expect(interrupted["source_after_interruption"]["integrity_check"] == "ok",
           "中断后原库完整性异常")
    expect(interrupted["recovery_body"]["matches_image_id"], "恢复采集摘要不一致")
    expect(interrupted["recovery_download"]["matches_image_id"], "恢复采集下载摘要不一致")


def human_report(report):
    lines = ["Pagefold WAL 采集演示", "=" * 72]
    denial = report["policy_denial"]
    lines.append(f"配置约束：未允许路径被拒绝 -> HTTP {denial['status']} {denial['response']['code']}")
    for scenario in report["scenarios"]:
        lines.extend(["", "-" * 72, scenario["title"]])
        if scenario["scenario"] == "steady_http":
            first, second = scenario["registration"], scenario["second_registration"]
            lines.append(f"首次采集：{first['image_id']}")
            lines.append(f"  {first['size']} B / {first['row_count']} 行 / {first['table_rows']}")
            lines.append(f"  包含提交 {first['included_commit_range']}，目录签名 {first['catalog_signature'][:16]}…")
            lines.append(f"再次采集：{second['image_id']} duplicate={second['duplicate']} "
                         f"same_id={first['image_id'] == second['image_id']}")
            lines.append(f"登记摘要校验：stored={scenario['stored_body']['matches_image_id']}，"
                         f"http_download={scenario['http_download']['matches_image_id']}，"
                         f"collection={scenario['stored_body']['matches_collection_digest']}")
            lines.append(f"原库前后是否变化：{scenario['source_changed']}；"
                         f"完整性 {scenario['source_after']['integrity_check']}；"
                         f"head={scenario['source_after']['head_seq']}")
        elif scenario["scenario"] == "concurrent_writes":
            reg = scenario["registration"]
            lines.append(f"一致镜像：{reg['image_id']}")
            lines.append(f"  {reg['size']} B / {reg['row_count']} 行 / {reg['table_rows']}")
            lines.append(f"  镜像内提交前缀 {reg['included_commit_range']}，共 {len(reg['included_commit_seqs'])} 个")
            lines.append(f"  采集后原库 head={scenario['source_final']['head_seq']}，"
                         f"未包含后缀 {scenario['excluded_commit_seqs']}")
            lines.append(f"  head 行与最后提交一致：{scenario['head_matches_last_included_commit']}")
            lines.append(f"  登记/下载摘要一致：stored={scenario['stored_body']['matches_image_id']}，"
                         f"http={scenario['http_download']['matches_image_id']}，"
                         f"凭证={scenario['persisted_collection_digest'] == reg['image_id']}")
            lines.append(f"  原库完整性：{scenario['source_final']['integrity_check']}")
        else:
            failure = scenario["failure"]
            lines.append(f"中断结果：HTTP 等价 {failure['status']} {failure['code']} — {failure['detail']}")
            lines.append(f"镜像登记数 {scenario['registrations_before_failure']} -> "
                         f"{scenario['registrations_after_failure']}；"
                         f"采集凭证数 {scenario['collections_before_failure']} -> "
                         f"{scenario['collections_after_failure']}")
            lines.append(f"失败尝试产生登记：{scenario['failed_attempt_created_registration']}")
            lines.append(f"原库仍可写可读：head={scenario['source_after_interruption']['head_seq']}，"
                         f"完整性 {scenario['source_after_interruption']['integrity_check']}")
            recovery = scenario["recovery_registration"]
            lines.append(f"恢复采集：{recovery['image_id']} duplicate={recovery['duplicate']} "
                         f"提交范围 {recovery['included_commit_range']}")
            lines.append(f"恢复登记摘要校验：stored={scenario['recovery_body']['matches_image_id']}，"
                         f"http={scenario['recovery_download']['matches_image_id']}")
    lines.extend(["", "=" * 72, f"最终登记镜像数：{len(report['final_images'])}，"
                                f"采集凭证数：{len(report['final_collections'])}"])
    return "\n".join(lines)


def markdown_report(report):
    lines = [
        "# Pagefold WAL 采集比较",
        "",
        "| 场景 | 结果 | 镜像摘要 | 规模 | 包含提交 | 登记/影响 |",
        "|---|---|---|---|---|---|",
    ]
    for scenario in report["scenarios"]:
        if scenario["scenario"] == "steady_http":
            r = scenario["registration"]
            impact = (f"重复采集 duplicate={scenario['second_registration']['duplicate']}；"
                      f"原库变化={scenario['source_changed']}；"
                      f"完整性={scenario['source_after']['integrity_check']}")
            lines.append("| 稳定 HTTP 采集 | 成功并复用 | "
                         f"`{r['image_id'][:16]}…` | {r['size']} B / {r['row_count']} 行 | "
                         f"{r['included_commit_range']} | {impact} |")
        elif scenario["scenario"] == "concurrent_writes":
            r = scenario["registration"]
            impact = (f"原库最终 head={scenario['source_final']['head_seq']}；"
                      f"排除后缀 {scenario['excluded_commit_seqs']}；"
                      f"一致性={scenario['head_matches_last_included_commit']}")
            lines.append("| 并发 WAL 写入 | 成功，时点由镜像内提交凭证确定 | "
                         f"`{r['image_id'][:16]}…` | {r['size']} B / {r['row_count']} 行 | "
                         f"{r['included_commit_range']} | {impact} |")
        else:
            f = scenario["failure"]
            r = scenario["recovery_registration"]
            impact = (f"登记数不变={not scenario['failed_attempt_created_registration']}；"
                      f"原库完整性={scenario['source_after_interruption']['integrity_check']}；"
                      f"恢复镜像 `{r['image_id'][:16]}…`")
            lines.append(f"| 读源中断 | {f['code']} / {f['status']}，无半成品登记 | — | — | — | {impact} |")
    lines.append("")
    lines.append(f"服务端拒绝非允许源：`{report['policy_denial']['response']['code']}` "
                 f"HTTP {report['policy_denial']['status']}。")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
