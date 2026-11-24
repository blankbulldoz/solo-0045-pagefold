"""移植入口的状态操作：创建移植、读取视图、保存保留/重新选择裁决。"""

import json
import uuid

from .codec import canonical, fingerprint
from .errors import Problem
from .images import get_image
from .merge import selected
from .port import (DISPOSITION_LABELS, PORT_ROLES, _disposition, build_groups,
                   calculate_port, summarize_group, unresolved_count)
from .rows import read_image
from .sqlite_image import mounted

PORT_CHOICES = (*PORT_ROLES, "clear")


def create_port(store, sources):
    with store.connect() as db:
        images = [get_image(db, sources[role], body=True) for role in PORT_ROLES]
    catalogs = [json.loads(image["catalog"]) for image in images]
    if len({catalog["signature"] for catalog in catalogs}) != 1:
        raise Problem("schema_mismatch", "三份镜像的模式记录不一致。", 409)
    snapshots = []
    for image, catalog in zip(images, catalogs):
        with mounted(image["body"]) as snapshot:
            snapshots.append(read_image(snapshot, catalog))
    view = calculate_port(snapshots, catalogs[0], {})
    port_id = uuid.uuid4().hex
    with store.connect(write=True) as db:
        db.execute("INSERT INTO ports(id,base,branch,target,items) VALUES(?,?,?,?,?)",
                   (port_id, sources["base"], sources["left"], sources["right"],
                    canonical(view["items"])))
        return read_port(db, port_id)


def get_port(db, port_id):
    row = db.execute("SELECT * FROM ports WHERE id=?", (port_id,)).fetchone()
    if row is None:
        raise Problem("unknown_port", "找不到指定的移植计划。", 404)
    return row


def port_content(row):
    return json.loads(row["items"]), json.loads(row["decisions"])


def rebuild_view(items, decisions, catalog):
    """基于已存储的三方条目重建处置视图与改名组（无需重读镜像快照）。"""
    group_map, order, groups = build_groups(items, catalog)
    detailed = []
    for item in items:
        disposition, source = _disposition(item, decisions)
        role, selected_row = selected(item, decisions)
        detailed.append({**item, "group": group_map.get(item["id"]),
                         "disposition": disposition, "source": source,
                         "choice": role, "selected": selected_row})
    rename_groups = [summarize_group(groups[gid], detailed) for gid in order]
    return {"items": detailed, "groups": rename_groups}


def read_port(db, port_id):
    row = get_port(db, port_id)
    items, decisions = port_content(row)
    catalog = json.loads(get_image(db, row["base"])["catalog"])
    view = rebuild_view(items, decisions, catalog)
    sources = {"base": row["base"], "left": row["branch"], "right": row["target"]}
    return {
        "id": port_id, "kind": "port", "sources": sources, "revision": row["revision"],
        "port_digest": fingerprint([[i["id"], i["disposition"], i["source"]]
                                    for i in view["items"]]),
        "unresolved": unresolved_count(items, decisions),
        "items": [dict(i, disposition_label=DISPOSITION_LABELS[i["disposition"]])
                  for i in view["items"]],
        "groups": view["groups"],
    }


def list_ports(db):
    return [dict(row) for row in db.execute("""
        SELECT p.id,p.revision,b.label AS base_label,m.label AS branch_label,t.label AS target_label
        FROM ports p JOIN images b ON b.id=p.base JOIN images m ON m.id=p.branch
        JOIN images t ON t.id=p.target ORDER BY p.rowid DESC
    """)]


def decide_port(store, port_id, revision, item_id, choice):
    with store.connect(write=True) as db:
        row = get_port(db, port_id)
        if row["revision"] != revision:
            raise Problem("stale_revision", "移植计划版本已改变，请刷新后再操作。", 409)
        items, decisions = port_content(row)
        if not any(item["id"] == item_id and item["automatic"] is None for item in items):
            raise Problem("not_conflict", "该条目不是可裁决的冲突。", 409)
        new_choice = None if choice == "clear" else choice
        if new_choice != decisions.get(item_id):
            if new_choice is None:
                decisions.pop(item_id, None)
            else:
                decisions[item_id] = new_choice
            revision += 1
            db.execute("UPDATE ports SET decisions=?,revision=? WHERE id=?",
                       (canonical(decisions), revision, port_id))
        return {"id": port_id, "revision": revision, "item": item_id, "choice": new_choice}
