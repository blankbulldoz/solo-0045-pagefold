import base64
import binascii
import json

from .codec import canonical, digest
from .errors import Problem
from .images import get_image, image_record, persist_image, prepare_image
from .inputs import fields, multipart
from .merge import ROLES
from .render import ROLE_LABELS
from .sqlite_image import MAX_BYTES

# 三份镜像各自不超过 4 MiB，再留出分部分隔与字段开销。
GROUP_MAX_BYTES = 3 * MAX_BYTES + (1 << 16)


def group_id(sources):
    """同一组角色映射稳定对应同一个组标识，重试不会产生第二组。"""
    return digest(canonical({role: sources[role] for role in ROLES}).encode("ascii"))


def _label(role, value):
    label = (value or "").strip()
    return (label[:100] or f"{ROLE_LABELS[role]}镜像")


def _json_entries(handler):
    data = fields(handler, GROUP_MAX_BYTES)
    entries = {}
    for role in ROLES:
        slot = data.get(role)
        if not isinstance(slot, dict):
            raise Problem("group_role", f"字段 {role} 须包含标签和镜像数据。", 400)
        raw = slot.get("data")
        if not isinstance(raw, str):
            raise Problem("group_role", f"字段 {role} 缺少 Base64 镜像数据。", 400)
        try:
            blob = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise Problem("group_encoding", f"{ROLE_LABELS[role]} 的镜像数据不是合法 Base64。", 400) from exc
        entries[role] = (blob, _label(role, slot.get("label")))
    extra = set(data) - set(ROLES)
    if extra:
        raise Problem("group_role", f"组登记只接受 {', '.join(ROLES)} 三个角色字段。", 400)
    return entries


def _multipart_entries(handler):
    parts = multipart(handler, GROUP_MAX_BYTES)
    entries = {}
    for name, filename, blob in parts:
        if name not in ROLES:
            raise Problem("group_role", f"未知的组角色字段：{name}。", 400)
        if name in entries:
            raise Problem("group_role", f"角色 {name} 在同一次提交中出现了多次。", 400)
        entries[name] = (blob, _label(name, filename))
    missing = [role for role in ROLES if role not in entries]
    if missing:
        raise Problem("group_role", "缺少角色镜像：" + "、".join(ROLE_LABELS[r] for r in missing) + "。", 400)
    return entries


def group_entries(handler):
    nature = handler.headers.get_content_type()
    if nature == "application/json":
        return _json_entries(handler)
    if nature and nature.startswith("multipart/form-data"):
        return _multipart_entries(handler)
    raise Problem("media_type", "组登记请提交 multipart 表单或 JSON。", 415)


def register_group(store, entries):
    if set(entries) != set(ROLES):
        raise Problem("group_role", "一次组登记须恰好包含祖本、左支、右支三个角色。", 400)
    # 先在写事务之外完整校验三份文件：任一份不合格都不产生任何登记。
    prepared = {}
    for role in ROLES:
        blob, label = entries[role]
        try:
            prepared[role] = (*prepare_image(blob), label, blob)
        except Problem as exc:
            raise Problem(exc.code, f"{ROLE_LABELS[role]}（{role}）：{exc.detail}", exc.status) from exc
    # 组本身要求三份模式精确匹配，避免把无法合流的组合落库。
    signatures = {role: json.loads(prepared[role][1])["signature"] for role in ROLES}
    if len(set(signatures.values())) != 1:
        differing = [ROLE_LABELS[role] for role in ROLES if signatures[role] != signatures["base"]]
        raise Problem("schema_mismatch",
                      "镜像组的模式记录不一致：" + "、".join(differing) + " 与祖本不匹配。", 409)
    with store.connect(write=True) as db:
        sources = {}
        for role in ROLES:
            checksum, catalog_text, label, blob = prepared[role]
            persist_image(db, checksum, label, catalog_text, blob)
            sources[role] = checksum
        gid = group_id(sources)
        db.execute("INSERT OR IGNORE INTO groups(id,base,left_id,right_id) VALUES(?,?,?,?)",
                   (gid, *(sources[role] for role in ROLES)))
        return read_group(db, gid)


def get_group(db, group_id_value):
    row = db.execute("SELECT * FROM groups WHERE id=?", (group_id_value,)).fetchone()
    if row is None:
        raise Problem("unknown_group", "找不到指定的镜像组。", 404)
    return row


def read_group(db, group_id_value):
    row = get_group(db, group_id_value)
    sources = {"base": row["base"], "left": row["left_id"], "right": row["right_id"]}
    images = {role: image_record(get_image(db, sources[role])) for role in ROLES}
    return {"id": row["id"], "sources": sources, "created_at": row["created_at"], "images": images}


def list_groups(db):
    rows = db.execute("""
        SELECT g.id,g.created_at,g.base,g.left_id,g.right_id,
               b.label AS base_label,l.label AS left_label,r.label AS right_label
        FROM groups g JOIN images b ON b.id=g.base JOIN images l ON l.id=g.left_id
        JOIN images r ON r.id=g.right_id ORDER BY g.rowid DESC
    """).fetchall()
    return [{"id": row["id"], "created_at": row["created_at"],
             "sources": {"base": row["base"], "left": row["left_id"], "right": row["right_id"]},
             "labels": {"base": row["base_label"], "left": row["left_label"], "right": row["right_label"]}}
            for row in rows]
