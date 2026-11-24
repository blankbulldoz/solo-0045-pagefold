import json
import uuid
from urllib.parse import urlencode

from .codec import canonical, fingerprint
from .errors import Problem
from .images import get_image
from .inputs import TOKEN, unsigned
from .merge import ROLES, calculate, selected
from .rows import read_image
from .sqlite_image import mounted

PAGE_LIMIT = 20
MAX_LIMIT = 100


def create_plan(store, sources):
    with store.connect() as db:
        images = [get_image(db, sources[role], body=True) for role in ROLES]
    catalogs = [json.loads(image["catalog"]) for image in images]
    if len({catalog["signature"] for catalog in catalogs}) != 1:
        raise Problem("schema_mismatch", "三份镜像的模式记录不一致。", 409)
    snapshots = []
    for image, catalog in zip(images, catalogs):
        with mounted(image["body"]) as snapshot:
            snapshots.append(read_image(snapshot, catalog))
    items = calculate(*snapshots)
    plan_id = uuid.uuid4().hex
    with store.connect(write=True) as db:
        db.execute("INSERT INTO plans(id,base,left_id,right_id,items) VALUES(?,?,?,?,?)",
                   (plan_id, *[sources[role] for role in ROLES], canonical(items)))
        return read_plan(db, plan_id)


def get_plan(db, plan_id):
    row = db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise Problem("unknown_plan", "找不到指定的合流计划。", 404)
    return row


def plan_content(row):
    return json.loads(row["items"]), json.loads(row["decisions"])


def annotate(items, decisions):
    result = []
    for position, item in enumerate(items):
        role, _ = selected(item, decisions)
        result.append(dict(item, position=position, choice=role, pending=role is None))
    return result


def table_summary(items):
    tables = {}
    for item in items:
        entry = tables.setdefault(item["table"], {"name": item["table"], "items": 0, "conflicts": 0,
                                                  "automatic": 0, "pending": 0, "decided": 0})
        entry["items"] += 1
        if item["automatic"] is None:
            entry["conflicts"] += 1
            entry["pending" if item["pending"] else "decided"] += 1
        else:
            entry["automatic"] += 1
    return list(tables.values())


def bounded_limit(value):
    limit = unsigned(value, "limit")
    if not 1 <= limit <= MAX_LIMIT:
        raise Problem("limit", f"每页行数须在 1 至 {MAX_LIMIT} 之间。", 400)
    return limit


def table_name(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 400:
        raise Problem("table", "表名无效。", 400)
    return value


def item_token(value, name):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise Problem(name, f"字段 {name} 须为计划条目标识。", 400)
    return value


def view_options(query, default_pending=True, default_limit=PAGE_LIMIT):
    pending = query.get("pending", "1" if default_pending else "0")
    if pending not in {"0", "1"}:
        raise Problem("pending", "字段 pending 须为 0 或 1。", 400)
    limit = bounded_limit(query["limit"]) if "limit" in query else default_limit
    revision = unsigned(query["revision"], "revision") if "revision" in query else None
    return {"table": table_name(query.get("table")), "pending": pending == "1", "limit": limit,
            "cursor": item_token(query.get("cursor"), "cursor"),
            "revision": revision, "saved": item_token(query.get("saved"), "saved")}


def view_fields(data):
    pending = data.get("pending", "0")
    if pending not in {"0", "1"}:
        raise Problem("pending", "字段 pending 须为 0 或 1。", 400)
    raw_limit = data.get("limit")
    return {"table": table_name(data.get("table")), "pending": pending == "1",
            "limit": bounded_limit(raw_limit) if raw_limit not in (None, "") else None,
            "cursor": item_token(data.get("cursor"), "cursor")}


def paged(plan_id, table=None, pending=True, limit=None, cursor=None, revision=None, saved=None):
    params = {}
    if table:
        params["table"] = table
    params["pending"] = "1" if pending else "0"
    if limit is not None:
        params["limit"] = limit
    if cursor:
        params["cursor"] = cursor
    if revision is not None:
        params["revision"] = revision
    if saved:
        params["saved"] = saved
    return f"/plans/{plan_id}?" + urlencode(params)


def continuation(db, plan_id, view, saved):
    row = get_plan(db, plan_id)
    items, decisions = plan_content(row)
    annotated = annotate(items, decisions)
    item = next((entry for entry in annotated if entry["id"] == saved), None)
    cursor = view["cursor"]
    if item is not None:
        if view["pending"] and item["pending"] and view["limit"] is not None:
            selection = [entry for entry in annotated
                         if (view["table"] is None or entry["table"] == view["table"]) and entry["pending"]]
            index = next(n for n, entry in enumerate(selection) if entry["id"] == saved)
            cursor = selection[index - 1]["id"] if index else None
        elif view["pending"] and not item["pending"]:
            cursor = item["id"]
    return paged(plan_id, table=view["table"], pending=view["pending"], limit=view["limit"],
                 cursor=cursor, revision=row["revision"], saved=saved if item is not None else None)


def read_plan(db, plan_id, table=None, pending=False, limit=None, cursor=None, revision=None,
              saved=None):
    row = get_plan(db, plan_id)
    items, decisions = plan_content(row)
    annotated = annotate(items, decisions)
    tables = table_summary(annotated)
    if table is not None and table not in {entry["name"] for entry in tables}:
        raise Problem("unknown_table", "计划中没有该表的差异行。", 404)
    counts = {"items": len(annotated),
              "automatic": sum(item["automatic"] is not None for item in annotated),
              "conflicts": sum(item["automatic"] is None for item in annotated),
              "pending": sum(item["pending"] for item in annotated)}
    counts["decided"] = counts["conflicts"] - counts["pending"]
    selection = [item for item in annotated
                 if (table is None or item["table"] == table) and (not pending or item["pending"])]
    scope_pending = next((entry["pending"] for entry in tables if entry["name"] == table),
                         counts["pending"]) if table is not None else counts["pending"]
    start = 0
    anchor_decided = False
    if cursor is not None:
        anchor = next((item for item in annotated if item["id"] == cursor), None)
        if anchor is None:
            raise Problem("cursor", "翻页位置不属于该计划。", 400)
        start = sum(1 for item in selection if item["position"] <= anchor["position"])
        anchor_decided = pending and not anchor["pending"]
    if limit is None:
        page, pages, page_number, following, previous = selection[start:], 1, 1, None, None
    else:
        page = selection[start:start + limit]
        pages = max(1, -(-len(selection) // limit))
        page_number = start // limit + 1 if start % limit == 0 else None
        following = page[-1]["id"] if start + len(page) < len(selection) else None
        if start == 0:
            previous = None
        else:
            prior = max(0, start - limit)
            previous = "" if prior == 0 else selection[prior - 1]["id"]
    saved_item = next((item for item in annotated if item["id"] == saved), None) if saved else None
    return {"id": plan_id, "sources": {"base": row["base"], "left": row["left_id"],
                                       "right": row["right_id"]},
            "revision": row["revision"], "state": row["state"],
            "plan_digest": fingerprint(items), "unresolved": counts["pending"], "items": page,
            "counts": counts, "tables": tables,
            "view": {"table": table, "pending": pending, "limit": limit, "cursor": cursor,
                     "start": start, "end": start + len(page), "page": page_number, "pages": pages,
                     "total": len(selection), "next": following, "previous": previous,
                     "anchor_decided": anchor_decided, "scope_pending": scope_pending,
                     "revision": revision, "current_revision": row["revision"],
                     "stale": revision is not None and revision != row["revision"]},
            "saved": None if saved_item is None else {
                "id": saved_item["id"], "table": saved_item["table"], "key": saved_item["key"],
                "choice": saved_item["choice"], "pending": saved_item["pending"]}}


def list_plans(db):
    return [dict(row) for row in db.execute("""
        SELECT p.id,p.revision,p.state,b.label AS base_label,l.label AS left_label,r.label AS right_label
        FROM plans p JOIN images b ON b.id=p.base JOIN images l ON l.id=p.left_id
        JOIN images r ON r.id=p.right_id ORDER BY p.rowid DESC
    """)]


def expect_revision(plan, revision):
    if plan["revision"] != revision:
        raise Problem("stale_revision", "计划版本已改变，请刷新后再操作。", 409)


def expect_open(plan):
    if plan["state"] != "open":
        raise Problem("closed_plan", "封存或归档后的计划不可继续编辑或重演。", 409)
