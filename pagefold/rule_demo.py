"""A fixed conflict batch for trying rule coverage, overlap and exceptions."""

import sqlite3

from .codec import canonical
from .images import register
from .plans import create_plan
from .rules import normalize_rule


SCHEMA = """
CREATE TABLE habit_case(
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    note TEXT,
    weight REAL,
    status TEXT NOT NULL
) WITHOUT ROWID;
"""


def scenario_image(flavor):
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO habit_case VALUES(?,?,?,?,?)", [
            ("delete-edit", "删除对修改", "右支将复核状态", 10.0, "new"),
            ("absent-null", "缺席对 SQL NULL", "右支会清空本备注", 1.0, "new"),
            ("weight", "权重归属", "共同备注", 10.0, "active"),
            ("null-weight", "权重清空例外", "共同备注", 10.0, "active"),
        ])
        if flavor == "left":
            db.execute("DELETE FROM habit_case WHERE code IN ('delete-edit', 'absent-null')")
            db.execute("UPDATE habit_case SET title='权重分歧：左支改标题' WHERE code='weight'")
            db.execute("UPDATE habit_case SET title='NULL 例外：左支也改标题' WHERE code='null-weight'")
        elif flavor == "right":
            db.execute("UPDATE habit_case SET status='reviewed' WHERE code='delete-edit'")
            db.execute("UPDATE habit_case SET note=NULL WHERE code='absent-null'")
            db.execute("UPDATE habit_case SET weight=12 WHERE code='weight'")
            db.execute("UPDATE habit_case SET weight=NULL WHERE code='null-weight'")
        db.commit()
        return db.serialize()
    finally:
        db.close()


RULES = [
    {
        "id": "habit-default-left",
        "name": "习惯表的广谱默认意见",
        "description": "覆盖整张 habit_case；优先级低，专门用于观察覆盖与重叠。",
        "category": "coverage",
        "priority": 10,
        "choice": "left",
        "tables": ["habit_case"],
        "when": {"op": "all", "conditions": []},
    },
    {
        "id": "delete-edit-prefer-survivor",
        "name": "左删右改时保留修改后的行",
        "description": "左支整行缺席而右支原地修改同键；高优先级覆盖广谱左支意见。",
        "category": "override",
        "priority": 100,
        "choice": "right",
        "tables": ["habit_case"],
        "when": {"op": "delete_vs_edit", "deleted_role": "left", "edited_role": "right"},
    },
    {
        "id": "null-note-is-cell-not-absence",
        "name": "右支把备注清空为 SQL NULL 不是删行",
        "description": "同时检查左支整行缺席与右支 note 单元格为 SQL NULL，示范两种“空”的边界。",
        "category": "overlap",
        "priority": 120,
        "choice": "right",
        "tables": ["habit_case"],
        "when": {"op": "all", "conditions": [
            {"op": "row_state", "role": "left", "state": "absent"},
            {"op": "column_state", "role": "right", "column": "note", "state": "sql_null"},
        ]},
    },
    {
        "id": "weight-column-owner-right",
        "name": "只有右支改权重时采纳右支",
        "description": "两支都改写同行，但只有右支的列级差异恰好是 weight。",
        "category": "overlap",
        "priority": 90,
        "choice": "right",
        "tables": ["habit_case"],
        "when": {"op": "all", "conditions": [
            {"op": "change_kind", "role": "left", "kind": "updated"},
            {"op": "change_kind", "role": "right", "kind": "updated"},
            {"op": "only_changed_columns", "role": "right", "columns": ["weight"]},
        ]},
    },
    {
        "id": "weight-stability-base",
        "name": "权重应保持稳定",
        "description": "只要右支改了 weight 就倾向祖本；与同优先级的权重归属规则形成重叠分歧。",
        "category": "overlap",
        "priority": 90,
        "choice": "base",
        "tables": ["habit_case"],
        "when": {"op": "all", "conditions": [
            {"op": "branch_changed", "role": "left", "kinds": ["updated"]},
            {"op": "column_changed", "role": "right", "column": "weight"},
        ]},
    },
    {
        "id": "title-owner-left",
        "name": "标题维护者倾向左支",
        "description": "左支修改 title 时给出左支意见；在重叠中优先级较低。",
        "category": "overlap",
        "priority": 80,
        "choice": "left",
        "tables": ["habit_case"],
        "when": {"op": "column_changed", "role": "left", "column": "title"},
    },
    {
        "id": "null-weight-exception-base",
        "name": "权重写成 SQL NULL 是例外，先回祖本",
        "description": "仅当右支行存在且 weight 单元格为 SQL NULL 时命中；它不是整行缺席。",
        "category": "exception",
        "priority": 200,
        "choice": "base",
        "tables": ["habit_case"],
        "when": {"op": "all", "conditions": [
            {"op": "change_kind", "role": "left", "kind": "updated"},
            {"op": "only_changed_columns", "role": "right", "columns": ["weight"]},
            {"op": "column_state", "role": "right", "column": "weight", "state": "sql_null"},
        ]},
    },
]


def seed_rule_demo(store):
    sources = {
        role: register(store, scenario_image(role), label)["id"]
        for role, label in (("base", "规则祖本"), ("left", "规则左支"), ("right", "规则右支"))
    }
    for spec in RULES:
        rule = normalize_rule(spec)
        with store.connect(write=True) as db:
            db.execute("INSERT OR IGNORE INTO rules(id,name,spec,builtin) VALUES(?,?,?,1)",
                       (rule["id"], rule["name"], canonical(rule)))
    return create_plan(store, sources)
