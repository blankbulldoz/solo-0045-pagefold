import json
from urllib.parse import unquote

from .archive import archive
from .attempts import list_attempts, rehearse
from .decisions import resolve
from .errors import Problem
from .images import get_image, image_record, list_images, register, table_page
from .inputs import fields, payload, token, unsigned
from .merge import ROLES
from .plans import create_plan, list_plans, read_plan
from .releases import get_release, seal
from .sqlite_image import MAX_BYTES
from .witness import witness_graph

WITNESS_TAIL = {"witness", "witnesses", "refs", "references", "neighbors",
                "graph", "lineage", "related", "trace"}


def read(store, parts, query):
    resource, *tail = parts
    with store.connect() as db:
        if resource in WITNESS_TAIL:
            # /api/witness、/api/witness/{image_id}、/api/witness/{image_id}/{table}
            image_id = tail[0] if len(tail) >= 1 else None
            table = tail[1] if len(tail) >= 2 else query.get("table")
            return witness_graph(db, image_id, table, query.get("key"), query=query)
        if resource == "images":
            if not tail:
                return list_images(db)
            if len(tail) == 1:
                return image_record(get_image(db, tail[0]))
            if len(tail) == 2 and tail[1] == "rows":
                result = table_page(db, tail[0], query.get("table"),
                                    unsigned(query.get("offset", "0"), "offset"))
                # 兼容把 key 直接带到 rows 入口的用法：附加同一镜像内的引用见证。
                if query.get("key") or query.get("pk"):
                    result["witness"] = witness_graph(
                        db, tail[0], query.get("table"), query.get("key"), query=query)
                return result
            if len(tail) >= 2 and tail[1] in WITNESS_TAIL:
                table = tail[2] if len(tail) >= 3 else query.get("table")
                return witness_graph(db, tail[0], table, query.get("key"), query=query)
        if resource == "plans":
            if not tail:
                return list_plans(db)
            if len(tail) == 1:
                return read_plan(db, tail[0])
            if len(tail) == 2 and tail[1] == "attempts":
                return list_attempts(db, tail[0])
            if len(tail) == 2 and tail[1] in {"download", "certificate"}:
                release = get_release(db, tail[0], body=tail[1] == "download")
                return release["candidate"] if tail[1] == "download" else json.loads(release["certificate"])
    raise Problem("route", "找不到该入口。", 404)


def upload(store, handler):
    if handler.headers.get_content_type() != "application/octet-stream":
        raise Problem("media_type", "请提交 application/octet-stream 格式的镜像。", 415)
    label = unquote(handler.headers.get("X-Image-Name", "")).strip() or "SQLite 镜像"
    return register(store, payload(handler, MAX_BYTES), label[:100])


def witness_post(store, handler, image_id=None, table=None):
    data = fields(handler, 32_768)
    image_id = image_id or data.get("image") or data.get("image_id")
    table = table or data.get("table") or data.get("relation")
    with store.connect() as db:
        return witness_graph(db, image_id, table, data.get("key"), query=data)


def write_plan(store, handler, tail):
    if not tail:
        data = fields(handler, 32_768)
        return create_plan(store, {role: token(data.get(role), role) for role in ROLES})
    if len(tail) != 2:
        raise Problem("route", "找不到该入口。", 404)
    plan_id, action = tail
    data = fields(handler, 32_768)
    revision = unsigned(data.get("revision"), "revision")
    if action == "decisions":
        choice = data.get("choice")
        if choice not in (*ROLES, "clear"):
            raise Problem("choice", "请选择祖本、左支、右支或清除裁决。", 400)
        return resolve(store, plan_id, revision, token(data.get("item"), "item"), choice)
    writes = {"rehearse": rehearse, "seal": seal, "archive": archive}
    if action in writes:
        return writes[action](store, plan_id, revision)
    raise Problem("route", "找不到该入口。", 404)
