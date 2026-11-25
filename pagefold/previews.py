import json
import re
import secrets

from .codec import canonical, fingerprint
from .errors import Problem
from .images import get_image
from .merge import ROLES
from .plans import insert_plan, prepare, read_plan

PREVIEW_TOKEN = re.compile(r"[a-f0-9]{64}\Z")

#: Difference volume that can still be reviewed directly on the plan page.
DIRECT_REVIEW_ITEMS = 200
#: Manual decisions that can still be handled in one direct review pass.
DIRECT_REVIEW_CONFLICTS = 50

CHANGE_KINDS = ("added", "deleted", "modified")
EFFECT_KINDS = ("insert", "delete", "update", "unchanged", "undecided")


def valid_token(value):
    return isinstance(value, str) and PREVIEW_TOKEN.fullmatch(value) is not None


def require_token(value):
    if not valid_token(value):
        raise Problem("preview_token", "预览凭证须为 64 位十六进制字符串。", 400)
    return value


def presence_kind(item):
    """Shape of the three-way difference relative to the common ancestor."""
    if item["base"] is None:
        return "added"
    if item["left"] is None or item["right"] is None:
        return "deleted"
    return "modified"


def automatic_effect(item):
    """Net row operation under the automatic choice (if any)."""
    role = item["automatic"]
    if role is None:
        return "undecided"
    row, base = item[role], item["base"]
    if base is None and row is not None:
        return "insert"
    if base is not None and row is None:
        return "delete"
    if row != base:
        return "update"
    return "unchanged"


def merge_shape(item):
    if item["automatic"] is None:
        return "conflict"
    if item["left"] == item["right"]:
        return "both_same"
    return "one_sided"


def summarize(items, images, repeated_labels=None):
    counts = {kind: 0 for kind in CHANGE_KINDS}
    resolution = {"automatic": 0, "conflict": 0}
    merge_types = {"one_sided": 0, "both_same": 0, "conflict": 0}
    effects = {kind: 0 for kind in EFFECT_KINDS}
    tables = {}
    for item in items:
        kind = presence_kind(item)
        counts[kind] += 1
        merge_types[merge_shape(item)] += 1
        bucket = tables.setdefault(item["table"], {
            "table": item["table"], "items": 0, "added": 0, "deleted": 0,
            "modified": 0, "automatic": 0, "conflict": 0})
        bucket["items"] += 1
        bucket[kind] += 1
        if item["automatic"]:
            resolution["automatic"] += 1
            bucket["automatic"] += 1
        else:
            resolution["conflict"] += 1
            bucket["conflict"] += 1
        effects[automatic_effect(item)] += 1
    total = len(items)
    conflicts = resolution["conflict"]
    automatic_writes = effects["insert"] + effects["delete"] + effects["update"]
    if repeated_labels is None:
        seen = {}
        for image in images:
            seen.setdefault(image["label"], set()).add(image["id"])
        repeated_labels = sorted(label for label, ids in seen.items() if len(ids) > 1)
    over_items = total > DIRECT_REVIEW_ITEMS
    over_conflicts = conflicts > DIRECT_REVIEW_CONFLICTS
    large = over_items or over_conflicts
    scale = {
        "level": "large" if large else "direct",
        "within_direct_review": not large,
        "large": large,
        "oversized": large,
        "direct_review_limit": DIRECT_REVIEW_ITEMS,
        "direct_conflict_limit": DIRECT_REVIEW_CONFLICTS,
        "item_limit": DIRECT_REVIEW_ITEMS,
        "conflict_limit": DIRECT_REVIEW_CONFLICTS,
        "over_item_limit": over_items,
        "over_conflict_limit": over_conflicts,
        "items": total,
        "conflicts": conflicts,
        "message": (
            f"差异量 {total} 项、待裁决 {conflicts} 项，超过直接审阅规模"
            f"（差异 > {DIRECT_REVIEW_ITEMS} 项或冲突 > {DIRECT_REVIEW_CONFLICTS} 项）。"
            if large else
            f"差异量 {total} 项、待裁决 {conflicts} 项，处于可直接审阅规模。"
        ),
    }
    actions = ["confirm", "revise"]
    available_actions = [
        {"id": "confirm", "kind": "primary", "label": "确认预览并创建正式计划",
         "description": "正式计划将逐字绑定本预览的三个来源与差异摘要。"},
        {"id": "revise", "kind": "back", "label": "返回调整选定角色",
         "href": "/", "description": "放弃本预览并返回首页，不会产生任何计划记录。"},
    ]
    if large:
        actions.append("reduce_scope")
        available_actions.append({
            "id": "reduce_scope", "kind": "advisory",
            "label": "缩小范围后重新登记",
            "description": "可取消本次创建，按表或批次拆分来源、重新登记后再次预览；"
                           "也可以仍要创建，随后在计划页集中处理冲突。",
        })
    estimate = {
        "items": total,
        "total": total,
        "differences": total,
        "item_count": total,
        "tables": len(tables),
        "table_count": len(tables),
        "automatic": resolution["automatic"],
        "conflicts": conflicts,
        "unresolved": conflicts,
        "manual_decisions": conflicts,
        "estimated_row_writes": automatic_writes,
        "automatic_row_changes": automatic_writes,
        "estimated_changes": automatic_writes,
        "max_row_writes": total,
        "max_changes": total,
    }
    return {
        "total": total,
        "item_count": total,
        "change_types": dict(counts, total=total),
        "changes": dict(counts, total=total),
        "merge_types": merge_types,
        "resolution": resolution,
        "effects": effects,
        "tables": [tables[name] for name in sorted(tables)],
        "estimate": estimate,
        "scale": scale,
        "review": dict(scale, actions=actions, available_actions=available_actions),
        "actions": actions,
        "available_actions": available_actions,
        "large": large,
        "oversized": large,
        "repeated_labels": repeated_labels,
        "labels_repeated": bool(repeated_labels),
        "labels": [image["label"] for image in images],
    }


def role_records(images):
    return {role: {"role": role, "id": image["id"], "label": image["label"],
                   "size": len(image["body"])}
            for role, image in zip(ROLES, images)}


def registry_repeated_labels(db, labels):
    """Selected labels under which the registry holds different byte digests."""
    labels = sorted(set(labels))
    if not labels:
        return []
    marks = ",".join("?" for _ in labels)
    rows = db.execute(
        f"SELECT label FROM images WHERE label IN ({marks}) GROUP BY label "
        "HAVING COUNT(DISTINCT id) > 1 ORDER BY label", labels).fetchall()
    return [row["label"] for row in rows]


def present(token, sources, images, items, metrics, digest, *, confirmed, plan_id=None):
    signature = json.loads(images[0]["catalog"])["signature"]
    total = metrics["total"]
    conflicts = metrics["resolution"]["conflict"]
    estimate = metrics["estimate"]
    return {
        **metrics,
        "preview_token": token,
        "token": token,
        "sources": dict(sources),
        "roles": role_records(images),
        "schema": signature,
        "schema_signature": signature,
        "items": items,
        "plan_digest": digest,
        "revision": 0,
        "state": "preview",
        "unresolved": conflicts,
        "confirmed": confirmed,
        "plan_id": plan_id,
        "stats": estimate,
        "volume": estimate,
        "estimated_volume": estimate,
        "counts": {"total": total, "unresolved": conflicts, "conflicts": conflicts,
                   "automatic": metrics["resolution"]["automatic"],
                   **metrics["change_types"]},
        "summary": {
            "sources": dict(sources),
            "roles": role_records(images),
            "plan_digest": digest,
            "schema": signature,
            "total": total,
            "item_count": total,
            **metrics["change_types"],
            "automatic": metrics["resolution"]["automatic"],
            "conflicts": conflicts,
            "unresolved": conflicts,
            "tables": estimate["tables"],
            "estimated_row_writes": estimate["estimated_row_writes"],
            "large": metrics["large"],
            "actions": metrics["actions"],
        },
    }


def build_preview(store, sources):
    images, items = prepare(store, sources)
    with store.connect() as db:
        repeated = registry_repeated_labels(db, [image["label"] for image in images])
    metrics = summarize(items, images, repeated)
    digest = fingerprint(items)
    token = secrets.token_hex(32)
    envelope = {"plan_digest": digest, "metrics": metrics}
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO plan_previews(token,base,left_id,right_id,items,summary) "
            "VALUES(?,?,?,?,?,?)",
            (token, sources["base"], sources["left"], sources["right"],
             canonical(items), canonical(envelope)))
    return present(token, sources, images, items, metrics, digest, confirmed=False)


def _load_envelope(row):
    envelope = json.loads(row["summary"])
    return envelope["metrics"], envelope["plan_digest"]


def _bound_sources(row):
    return {"base": row["base"], "left": row["left_id"], "right": row["right_id"]}


def read_preview(db, token):
    require_token(token)
    row = db.execute("SELECT * FROM plan_previews WHERE token=?", (token,)).fetchone()
    if row is None:
        raise Problem("unknown_preview", "找不到该预览，它可能已过期，请返回重新计算。", 404)
    sources = _bound_sources(row)
    items = json.loads(row["items"])
    metrics, digest = _load_envelope(row)
    images = [get_image(db, sources[role], body=True) for role in ROLES]
    return present(token, sources, images, items, metrics, digest,
                   confirmed=row["plan_id"] is not None, plan_id=row["plan_id"])


def confirm_preview(store, token, sources=None):
    require_token(token)
    with store.connect(write=True) as db:
        row = db.execute("SELECT * FROM plan_previews WHERE token=?", (token,)).fetchone()
        if row is None:
            raise Problem("unknown_preview", "找不到该预览，它可能已过期，请返回重新计算。", 404)
        bound = _bound_sources(row)
        if sources is not None and sources != bound:
            raise Problem(
                "preview_binding",
                "提交的来源与预览绑定的角色或字节摘要不一致，已阻止创建计划；"
                "请回到对应预览窗口确认，或重新生成预览。", 409)
        if row["plan_id"]:
            # Two windows confirming the same preview converge on one plan.
            plan = read_plan(db, row["plan_id"])
            plan["preview_token"] = token
            plan["already_confirmed"] = True
            plan["idempotent"] = True
            return plan
        items = json.loads(row["items"])
        _metrics, digest = _load_envelope(row)
        if fingerprint(items) != digest:
            raise Problem("preview_stale", "预览内容已损坏或过期，请重新生成预览。", 409)
        plan_id = insert_plan(db, bound, items)
        db.execute("UPDATE plan_previews SET plan_id=? WHERE token=?", (plan_id, token))
        plan = read_plan(db, plan_id)
        plan["preview_token"] = token
        plan["already_confirmed"] = False
        return plan
