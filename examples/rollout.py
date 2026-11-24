"""前进增量演示：两条合流线各封存两版成果，生成可验证增量并在接收方副本上应用。

接收方甲持有页布局不同但逻辑等价的副本，增量照常应用；
接收方乙的本地内容偏离增量起点，应用被拒绝且文件保持原样。
"""
import argparse
import sqlite3
import tempfile
from pathlib import Path

from pagefold.attempts import rehearse
from pagefold.catalog import describe
from pagefold.codec import canonical, digest
from pagefold.constraints import check_database
from pagefold.decisions import resolve
from pagefold.demo import image
from pagefold.errors import Problem
from pagefold.images import register
from pagefold.increments import apply_increment, build_increment, logical_digest
from pagefold.plans import create_plan
from pagefold.releases import get_release, seal
from pagefold.rows import read_image
from pagefold.sqlite_image import mounted, validate_header
from pagefold.store import Store

LEDGER_SCHEMA = """
CREATE TABLE account(id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL);
CREATE TABLE entry(id INTEGER PRIMARY KEY, account INTEGER NOT NULL REFERENCES account(id),
    memo TEXT, amount REAL NOT NULL CHECK(amount >= 0));
CREATE INDEX entry_account ON entry(account);
"""

# 两条记录交换唯一键值：经临时值分三步，避免中途撞上 UNIQUE 约束。
SWAP_LABEL = [("UPDATE bench SET label='暂存标签' WHERE code='north'", ()),
              ("UPDATE bench SET label='玄石' WHERE code='south'", ()),
              ("UPDATE bench SET label='右支更名' WHERE code='north'", ())]
SWAP_EMAIL = [("UPDATE account SET email='swap@example' WHERE id=1", ()),
              ("UPDATE account SET email='alpha@example' WHERE id=2", ()),
              ("UPDATE account SET email='beta@example' WHERE id=1", ())]


def expect(condition, message):
    if not condition:
        raise RuntimeError(message)


def edit(data, *statements):
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(data)
        for sql, params in statements:
            db.execute(sql, params)
        db.commit()
        return db.serialize()
    finally:
        db.close()


def relayout(data, page_size):
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(data)
        db.execute(f"PRAGMA page_size={page_size}")
        db.execute("VACUUM")
        return db.serialize()
    finally:
        db.close()


def ledger_image():
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(LEDGER_SCHEMA)
        db.executemany("INSERT INTO account VALUES(?,?,?)",
                       [(1, "alpha@example", "甲"), (2, "beta@example", "乙")])
        db.executemany("INSERT INTO entry VALUES(?,?,?,?)",
                       [(1, 1, "期初", 10.0), (2, 2, "期初", 20.0)])
        db.commit()
        return db.serialize()
    finally:
        db.close()


def merge_and_seal(store, sources, choices=()):
    plan = create_plan(store, sources)
    revision, pending = plan["revision"], list(choices)
    for item in plan["items"]:
        if item["automatic"] is None:
            revision = resolve(store, plan["id"], revision, item["id"], pending.pop(0))["revision"]
    expect(not pending, "预留的裁决没有用完")
    result = rehearse(store, plan["id"], revision)
    expect(result["status"] == "passed", f"重演未通过：{result}")
    return plan["id"], seal(store, plan["id"], revision)


def release_body(store, plan_id):
    with store.connect() as db:
        return get_release(db, plan_id, body=True)["candidate"]


def logical_rows(data):
    with mounted(data) as db:
        return read_image(db, describe(db))


def apply_to_file(path, increment):
    result = apply_increment(path.read_bytes(), increment)
    staging = path.with_name(path.name + ".tmp")
    staging.write_bytes(result)
    staging.replace(path)
    return result


def seal_two_rounds(store):
    quartz = register(store, image("quartz"), "quartz")["id"]
    sources = {"base": quartz, "left": register(store, image("cerulean"), "cerulean")["id"],
               "right": register(store, image("umber"), "umber")["id"]}
    plan_a1, cert_a1 = merge_and_seal(store, sources, ["right"])
    a1 = release_body(store, plan_a1)
    round_two = {"base": register(store, a1, "甲线二轮祖本")["id"],
                 "left": register(store, edit(a1, *SWAP_LABEL,
                                              ("UPDATE node SET note='左支注释' "
                                               "WHERE bench='south' AND slot=1", ())), "甲线二轮左支")["id"],
                 "right": register(store, edit(a1, *SWAP_LABEL,
                                               ("UPDATE node SET weight=3.5 "
                                                "WHERE bench='north' AND slot=1", ())), "甲线二轮右支")["id"]}
    plan_a2, cert_a2 = merge_and_seal(store, round_two)

    ledger = ledger_image()
    round_one = {"base": register(store, ledger, "乙线祖本")["id"],
                 "left": register(store, edit(ledger, ("INSERT INTO entry VALUES(3,1,'左支记账',5.0)", ())),
                                  "乙线一轮左支")["id"],
                 "right": register(store, edit(ledger, ("UPDATE account SET name='乙改' WHERE id=2", ())),
                                   "乙线一轮右支")["id"]}
    plan_b1, cert_b1 = merge_and_seal(store, round_one)
    b1 = release_body(store, plan_b1)
    round_two = {"base": register(store, b1, "乙线二轮祖本")["id"],
                 "left": register(store, edit(b1, *SWAP_EMAIL), "乙线二轮左支")["id"],
                 "right": register(store, edit(b1, *SWAP_EMAIL,
                                               ("INSERT INTO entry VALUES(4,2,'右支补记',7.5)", ())),
                                   "乙线二轮右支")["id"]}
    plan_b2, cert_b2 = merge_and_seal(store, round_two)
    return (plan_a1, cert_a1, plan_a2, cert_a2), (plan_b1, cert_b1, plan_b2, cert_b2)


def report_increment(name, increment, certificate):
    operations = sum(len(entry["delete"]) + len(entry["insert"]) for entry in increment["tables"])
    expect(increment["to"]["candidate_digest"] == certificate["candidate_digest"],
           f"{name} 线增量没有绑定新版封存凭证")
    print(f"  {name} 线：{len(increment['tables'])} 张表、{operations} 个行操作，"
          f"增量摘要 {digest(canonical(increment).encode('ascii'))[:16]}…")
    print(f"    起点 {increment['from']['candidate_digest'][:12]}… "
          f"逻辑 {increment['from']['logical'][:12]}…（{increment['from']['rows']} 行）")
    print(f"    目标 {increment['to']['candidate_digest'][:12]}… "
          f"逻辑 {increment['to']['logical'][:12]}…（{increment['to']['rows']} 行）")


def verify_result(name, data, sealed):
    validate_header(data)
    with mounted(data) as db:
        check_database(db)
        rows = read_image(db, describe(db))
    expect(rows == logical_rows(sealed), f"接收方{name}的逻辑内容应等于新版封存成果")
    layout = "字节一致" if digest(data) == digest(sealed) else "页布局不同、逻辑数据一致"
    print(f"  接收方{name}：完整性与外键检查通过，与新版封存成果{layout}")


def main():
    parser = argparse.ArgumentParser(description="封存成果的前进增量演示")
    parser.add_argument("--out", type=Path, help="保存增量文档与接收方文件的目录")
    args = parser.parse_args()
    root = args.out or Path(tempfile.mkdtemp(prefix="pagefold-rollout-"))
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "state.sqlite")

    print("一、两条合流线各封存两版成果（第二轮在两条记录间交换唯一键值）")
    (plan_a1, cert_a1, plan_a2, cert_a2), (plan_b1, cert_b1, plan_b2, cert_b2) = seal_two_rounds(store)
    a1, a2 = release_body(store, plan_a1), release_body(store, plan_a2)
    b1, b2 = release_body(store, plan_b1), release_body(store, plan_b2)
    for line, cert in (("甲一", cert_a1), ("甲二", cert_a2), ("乙一", cert_b1), ("乙二", cert_b2)):
        print(f"  {line}版封存：候选 {cert['candidate_digest'][:16]}…（{cert['candidate_size']} 字节）")

    print("二、为两份上一版封存成果生成前进增量")
    inc_a = build_increment(store, plan_a1, plan_a2)
    inc_b = build_increment(store, plan_b1, plan_b2)
    report_increment("甲", inc_a, cert_a2)
    report_increment("乙", inc_b, cert_b2)
    for name, increment in (("increment-a.json", inc_a), ("increment-b.json", inc_b)):
        (root / name).write_text(canonical(increment) + "\n", encoding="ascii")

    print("三、接收方甲：页布局不同但逻辑等价的副本，可以应用")
    copy_a = relayout(a1, 1024)
    expect(digest(copy_a) != digest(a1), "等价副本的字节应当与封存成果不同")
    expect(logical_digest(logical_rows(copy_a)) == inc_a["from"]["logical"],
           "等价副本的逻辑摘要应当匹配增量起点")
    path_a = root / "receiver-a.sqlite"
    path_a.write_bytes(copy_a)
    print(f"  副本字节 {digest(copy_a)[:16]}… 与封存字节 {digest(a1)[:16]}… 不同，逻辑摘要一致")
    result_a = apply_to_file(path_a, inc_a)
    with mounted(result_a) as db:
        labels = db.execute("SELECT code,label FROM bench ORDER BY code").fetchall()
    expect(labels == [("north", "右支更名"), ("south", "玄石")], "唯一标签交换结果不符")
    print(f"  应用成功，唯一标签已交换：{labels}")

    print("四、接收方乙：本地内容偏离增量起点，拒绝且文件不变")
    drift = edit(b1, ("UPDATE entry SET memo='本地私改' WHERE id=1", ()))
    path_b = root / "receiver-b.sqlite"
    path_b.write_bytes(drift)
    try:
        apply_increment(drift, inc_b)
        raise RuntimeError("偏离起点的副本不应接受增量")
    except Problem as exc:
        expect(exc.code == "base_mismatch", f"拒绝原因应为 base_mismatch，实际为 {exc.code}")
        print(f"  拒绝：{exc.code} — {exc.detail}")
    expect(path_b.read_bytes() == drift, "被拒绝后接收方文件应保持原样")
    print("  文件字节保持原样")

    print("五、对照：内容忠实的乙线副本接受同一份增量")
    path_b2 = root / "receiver-b-pristine.sqlite"
    path_b2.write_bytes(b1)
    result_b = apply_to_file(path_b2, inc_b)
    with mounted(result_b) as db:
        emails = db.execute("SELECT id,email FROM account ORDER BY id").fetchall()
    expect(emails == [(1, "beta@example"), (2, "alpha@example")], "唯一邮箱交换结果不符")
    print(f"  应用成功，唯一邮箱已交换：{emails}")

    print("六、核对迁移结果的关系约束与新版成果语义")
    verify_result("甲", result_a, a2)
    verify_result("丙", result_b, b2)
    print(f"全部核对通过，产物位于 {root}")


if __name__ == "__main__":
    main()
