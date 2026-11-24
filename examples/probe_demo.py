"""试算探索的合成镜像与工作流检查。

演示两种输入规模：
* 小规模（4 个冲突、每项两个合理选法，共 16 个组合）可在预算内完全穷举；
* 大规模（16 个冲突、65,536 个组合）预算无法穷举，只返回带覆盖说明的证据。

所有镜像在内存中生成并通过真实 HTTP 入口登记，检查脚本本身不写入任何计划
裁决；脚本结束后临时状态库自动销毁。

运行：python3 -m examples.probe_demo small|large|isolation|all
"""
import argparse
import json
import sqlite3
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pagefold.server import make_server
from pagefold.store import Store

SCHEMA = """
CREATE TABLE parent(code TEXT PRIMARY KEY, label TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE child(
    code TEXT PRIMARY KEY, parent TEXT REFERENCES parent(code),
    note TEXT, score REAL NOT NULL CHECK(score >= 0)
) WITHOUT ROWID;
CREATE TABLE loose(
    code TEXT PRIMARY KEY, child TEXT, note TEXT,
    FOREIGN KEY(child) REFERENCES child(code)
) WITHOUT ROWID;
CREATE TABLE coupled_parent(code TEXT PRIMARY KEY, label TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE coupled_child(
    code TEXT PRIMARY KEY, parent TEXT REFERENCES coupled_parent(code), note TEXT
) WITHOUT ROWID;
CREATE TABLE singleton(
    code TEXT PRIMARY KEY, tag TEXT NOT NULL UNIQUE, note TEXT
) WITHOUT ROWID;
"""


def build_image(flavor, conflicts=0):
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO parent VALUES(?,?)", [("p1", "祖本父行"), ("p2", "锚点父行")])
        db.executemany("INSERT INTO child VALUES(?,?,?,?)", [
            ("c1", "p1", "祖本子行", 1.0), ("c2", "p2", "稳定子行", 2.0)])
        db.executemany("INSERT INTO loose VALUES(?,?,?)", [
            ("l1", "c1", "祖本挂接"), ("l2", "c2", "稳定挂接")])
        db.executemany("INSERT INTO coupled_parent VALUES(?,?)", [("k1", "耦合父行")])
        db.executemany("INSERT INTO coupled_child VALUES(?,?,?)", [("x1", "k1", "耦合子行")])
        # 祖本先放入独立行，两个分支再各自改成同一 UNIQUE 标签，形成双边不同冲突。
        for n in range(conflicts):
            db.execute("INSERT INTO singleton VALUES(?,?,?)",
                       (f"ind{n}", f"祖本标签{n}", f"祖本独立{n}"))
        if flavor == "left":
            db.execute("UPDATE parent SET label='左支父行' WHERE code='p1'")
            db.execute("UPDATE child SET note='左支子行' WHERE code='c1'")
            db.execute("UPDATE loose SET note='左支挂接' WHERE code='l1'")
            # 与外键链完全无关的独立冲突：l2 挂在稳定行 c2 上，两个选法都可交付。
            db.execute("UPDATE loose SET note='左支独立备注' WHERE code='l2'")
            db.execute("UPDATE coupled_parent SET label='左支耦合父行' WHERE code='k1'")
            db.execute("UPDATE coupled_child SET note='左支耦合子行' WHERE code='x1'")
        elif flavor == "right":
            # 右支镜像自身仍通过完整性检查：先解除指向 p1 的引用再删除父行，
            # 而左支保留引用。合并时"父行是否存活"就决定了另外两项的外键命运。
            db.execute("UPDATE child SET parent=NULL WHERE code='c1'")
            db.execute("UPDATE loose SET child=NULL WHERE code='l1'")
            db.execute("DELETE FROM parent WHERE code='p1'")
            db.execute("UPDATE loose SET note='右支挂接' WHERE code='l1'")
            db.execute("UPDATE loose SET note='右支独立备注' WHERE code='l2'")
            # 耦合对：右支同时解除子行引用并删除父行，镜像内部自洽；
            # 但合并后"只删父行、保留子行引用"或"只解除子行、保留父行"都成立，
            # 两个右支选法必须同时采用才不会留下悬空外键——这是双因素条件依赖。
            db.execute("UPDATE coupled_child SET parent=NULL WHERE code='x1'")
            db.execute("DELETE FROM coupled_parent WHERE code='k1'")
        # 大规模输入：额外制造相互独立的唯一约束冲突，每个冲突仅左右两个选法。
        # 两个分支各自合法（同一主键改成同一 tag、不同 note），且都与祖本不同，
        # 形成双边不同冲突；选左或选右分别可交付，与外键链上的冲突完全独立。
        for n in range(conflicts):
            code = f"ind{n}"
            tag = f"共用标签{n}"
            if flavor == "left":
                db.execute("UPDATE singleton SET tag=?,note=? WHERE code=?",
                           (tag, f"左支独立{n}", code))
            elif flavor == "right":
                db.execute("UPDATE singleton SET tag=?,note=? WHERE code=?",
                           (tag, f"右支独立{n}", code))
        db.commit()
        return db.serialize()
    finally:
        db.close()


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200):
        headers = {}
        if data is not None:
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=data, headers=headers)
        try:
            with urlopen(request, timeout=20) as result:
                body, actual, kind = result.read(), result.status, result.headers.get_content_type()
        except HTTPError as error:
            with error:
                body, actual, kind = error.read(), error.code, error.headers.get_content_type()
        if actual != status:
            raise AssertionError(f"{route}: {actual} != {status}: {body[:400]!r}")
        return json.loads(body) if kind == "application/json" else body

    def upload(self, name, data):
        request = Request(self.origin + "/api/images", data=data, headers={
            "X-Image-Name": name, "Content-Type": "application/octet-stream"})
        with urlopen(request, timeout=20) as result:
            return json.loads(result.read())


def make_plan(client, conflicts):
    base = client.upload("base", build_image("base", conflicts))
    left = client.upload("left", build_image("left", conflicts))
    right = client.upload("right", build_image("right", conflicts))
    return client.call("/api/plans", {"base": base["id"], "left": left["id"], "right": right["id"]}, 201)


def expect(condition, description):
    if not condition:
        raise RuntimeError(description)
    print(f"  ✓ {description}")


def summarize(report):
    return (f"试算 {report['budget_used']}/{report['coverage']['total_combinations']} 个组合："
            f"{report['passed_combinations']} 可交付，{report['blocked_combinations']} 受阻，"
            f"穷举={report['coverage']['exhaustive']}")


def run_small(client):
    print("小规模输入：6 个冲突 × 2 个选法 = 64 个组合，预算内可穷举")
    plan = make_plan(client, conflicts=0)
    route = f"/api/plans/{plan['id']}"
    expect(plan["unresolved"] == 6, f"计划含 6 个待裁决冲突（实际 {plan['unresolved']}）")
    domain = ["left", "right"]
    report = client.call(route + "/probe", {"revision": 0, "budget": 64, "domain": domain})
    print("   ", summarize(report))
    conflicts = [item for item in plan["items"] if item["automatic"] is None]
    coverage = report["coverage"]
    expect(coverage["exhaustive"] and coverage["tested_combinations"] == 64
           and coverage["untested_count"] == 0, "64 个组合全部覆盖，无遗漏")
    expect(len({t["index"] for t in report["trials"]}) == 64, "每个组合只有一份证据")
    expect(report["passed_combinations"] == 36 and report["blocked_combinations"] == 28,
           "穷举证据：36 个组合可交付，28 个组合受阻")
    fk_blocked = [t for t in report["trials"]
                  if t.get("problem", {}).get("code") == "foreign_key"]
    expect(len(fk_blocked) == 28, "全部受阻均由外键检查给出可查阅诊断")
    # 条件依赖证据：受阻组合与最近通过组合的距离、涉及的冲突项和外键诊断都可回查。
    fk_interactions = [i for i in report["interactions"] if i["foreign_key"]]
    expect(fk_interactions, "每个外键受阻组合都附带可回查的条件依赖证据（受阻/通过组合对）")
    multi_factor = [i for i in fk_interactions if i["conditional"]]
    expect(multi_factor and all(i["distance"] >= 2 for i in multi_factor),
           "存在需要多项配合才能解除的受阻组合，单翻转无法解除（多因素条件依赖）")
    coupled = {item["id"] for item in conflicts if item["table"].startswith("coupled_")}
    expect(any(coupled & set(i["depends_on"]) for i in multi_factor),
           "多因素依赖定位到耦合链上的冲突项（删除父行类操作的叠加）")
    # 耦合对的四象限矩阵：只有"删父行但保留子行引用"(R,L) 外键受阻。
    cp = next(item["id"] for item in conflicts if item["table"] == "coupled_parent")
    cc = next(item["id"] for item in conflicts if item["table"] == "coupled_child")
    matrix = {}
    for trial in report["trials"]:
        chosen = {p["item"]: p["choice"] for p in trial["assignment"]}
        if all(p["choice"] == "left" for p in trial["assignment"]
               if p["item"] not in (cp, cc)):
            matrix[(chosen[cp], chosen[cc])] = trial["status"]
    expect(matrix[("left", "left")] == "passed" and matrix[("left", "right")] == "passed"
           and matrix[("right", "right")] == "passed"
           and matrix[("right", "left")] == "blocked",
           "证据矩阵：父行被删时子行引用才悬空——一条选项改变另一条的外键存活")
    impact_tables = {f["table"] for f in report["findings"]}
    neutral_tables = {f["table"] for f in report["neutral_items"]}
    expect(impact_tables == {"parent", "child", "coupled_parent", "coupled_child"},
           "4 个处于外键链上的冲突项会改变可交付性")
    expect(neutral_tables == {"loose"}, "loose 表的两个冲突完全独立，不改变可交付性")
    # 指定单一组合检查：右支删 p1 父行 + 左支保留 child 引用，必然外键受阻。
    parent_item = next(item for item in conflicts if item["table"] == "parent")
    child_item = next(item for item in conflicts if item["table"] == "child")
    assignment = {item["id"]: "right" for item in conflicts}
    assignment[child_item["id"]] = "left"
    assert assignment[parent_item["id"]] == "right"
    single = client.call(route + "/probe",
                         {"revision": 0, "budget": 1, "domain": domain, "assignments": assignment})
    expect(single["budget_used"] == 1 and single["trials"][0]["status"] == "blocked"
           and single["trials"][0]["problem"]["code"] == "foreign_key",
           "预算 1 且指定组合时只试算该组合，并返回外键受阻证据")
    return plan, report


def run_large(client):
    print("大规模输入：16 个冲突 × 2 个选法 = 65,536 个组合，预算无法穷举")
    plan = make_plan(client, conflicts=10)
    route = f"/api/plans/{plan['id']}"
    expect(plan["unresolved"] == 16, f"计划含 16 个待裁决冲突（实际 {plan['unresolved']}）")
    report = client.call(route + "/probe",
                         {"revision": 0, "budget": 40, "domain": ["left", "right"]})
    print("   ", summarize(report))
    coverage = report["coverage"]
    expect(not coverage["exhaustive"], "报告明确声明组合空间未穷举")
    expect(coverage["tested_combinations"] == 40 and coverage["untested_count"] == 65536 - 40,
           "实际试算数受预算限制，未覆盖数量精确给出")
    expect(len(coverage["untested_sample"]) <= 8 and coverage["untested_indices"] == [],
           "未覆盖空间过大时只给抽样序号，不伪造完整清单")
    expect(all(item["complete"] for item in coverage["items"]),
           "每个冲突项的两个选法都至少在一个已试组合中出现（单因素翻转保证）")
    expect(coverage["pair_coverage"]["tested"] < coverage["pair_coverage"]["total"],
           "成对覆盖率如实报告，未覆盖的选项对可以定位")
    expect(report["blocked_combinations"] > 0 and report["passed_combinations"] > 0,
           "已试组合中同时存在可交付与受阻样本，差异分析有证据支撑")
    finding_tables = {f["table"] for f in report["findings"]}
    expect({"parent", "child", "coupled_parent"} <= finding_tables,
           "外键链上的冲突被识别为可交付性差异（采样预算下至少三项有翻转证据）")
    neutral_tables = {f["table"] for f in report["neutral_items"]}
    expect({"singleton", "loose"} <= neutral_tables,
           "10 个 singleton 独立冲突与 loose 冲突不改变外键可交付性，如实归为中立")
    neutral_singleton = [f for f in report["neutral_items"] if f["table"] == "singleton"]
    expect(len(neutral_singleton) == 10, "全部 10 个完全独立的冲突项均有单翻转观察证据")
    # 预算上限保护。
    over = client.call(route + "/probe", {"revision": 0, "budget": 1000}, status=400)
    expect(over["code"] == "probe_budget", "超出上限的预算被拒绝")
    bad_domain = client.call(route + "/probe",
                             {"revision": 0, "budget": 4, "domain": ["base", "side"]}, status=400)
    expect(bad_domain["code"] == "probe_domain", "非法选项域被拒绝")
    # 指定组合缺项时拒绝，不产生半截试算。
    conflicts = [item for item in plan["items"] if item["automatic"] is None]
    partial = {item["id"]: "left" for item in conflicts[:-1]}
    missing = client.call(route + "/probe",
                          {"revision": 0, "budget": 4, "domain": ["left", "right"],
                           "assignments": partial}, status=400)
    expect(missing["code"] == "probe_assignment", "未覆盖全部冲突项的指定组合被拒绝")
    return plan, report


def run_isolation(client):
    print("隔离性核对：试算不写入当前计划的裁决、版本或重演历史")
    plan, _ = run_small(client)
    route = f"/api/plans/{plan['id']}"
    before = client.call(route)
    before_attempts = client.call(route + "/attempts")
    client.call(route + "/probe", {"revision": 0, "budget": 16, "domain": ["left", "right"]})
    client.call(route + "/probe", {"revision": 0, "budget": 3})
    after = client.call(route)
    after_attempts = client.call(route + "/attempts")
    expect(after["revision"] == before["revision"] == 0, "计划版本未被试算推进")
    expect(after["unresolved"] == before["unresolved"] == 6, "待裁决数量未变，没有隐式裁决写入")
    expect(after_attempts == before_attempts == [], "重演历史没有新增试算记录")
    # 封存后的计划依然可以只读试算，而正式裁决/重演仍被拒绝。
    conflicts = [item for item in after["items"] if item["automatic"] is None]
    for n, item in enumerate(conflicts):
        result = client.call(route + "/decisions",
                             {"revision": n, "item": item["id"], "choice": "left"})
        expect(result["revision"] == n + 1, "正式裁决按版本推进")
    current = client.call(route)
    rehearse = client.call(route + "/rehearse", {"revision": current["revision"]})
    expect(rehearse["status"] == "passed", "全部选左支的正式计划重演通过")
    client.call(route + "/seal", {"revision": current["revision"]})
    probe_after_seal = client.call(route + "/probe",
                                   {"revision": current["revision"], "budget": 4,
                                    "domain": ["left", "right"]})
    expect(probe_after_seal["read_only"] is True and probe_after_seal["budget_used"] == 4,
           "封存后的计划仍可只读试算")
    locked = client.call(route + "/decisions",
                         {"revision": current["revision"], "item": conflicts[0]["id"], "choice": "right"},
                         status=409)
    expect(locked["code"] == "closed_plan", "封存后的正式裁决仍被拒绝，试算没有改变写保护")
    stale = client.call(route + "/probe", {"revision": 999, "budget": 4}, status=409)
    expect(stale["code"] == "stale_revision", "试算同样校验版本，过期请求不会被执行")


def main():
    parser = argparse.ArgumentParser(description="Pagefold 试算探索演示与检查")
    parser.add_argument("goal", choices=("small", "large", "isolation", "all"))
    with tempfile.TemporaryDirectory(prefix="pagefold-probe-") as root:
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(server.server_port)
                goal = parser.parse_args().goal
                if goal in ("small", "all"):
                    run_small(client)
                if goal in ("large", "all"):
                    run_large(client)
                if goal in ("isolation", "all"):
                    run_isolation(client)
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
