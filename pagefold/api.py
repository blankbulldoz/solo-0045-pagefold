import json
from urllib.parse import unquote

from .archive import archive
from .attempts import compare_attempts, list_attempts, rehearse
from .compare import requested_pair, wants_verify
from .decisions import resolve
from .errors import Problem
from .images import get_image, image_record, list_images, register, table_page
from .inputs import fields, payload, token, unsigned
from .merge import ROLES
from .plans import create_plan, list_plans, read_plan
from .releases import get_release, seal
from .sqlite_image import MAX_BYTES


def _compare(db, plan_id, query, a=None, b=None):
    if a is None or b is None:
        query_a, query_b = requested_pair(query)
        a = query_a if a is None else a
        b = query_b if b is None else b
    if a is None or b is None:
        raise Problem("compare_revisions", "请提供要比较的两版重演记录（a 与 b）。", 400)
    return compare_attempts(db, plan_id, a, b, verify=wants_verify(query))


def read(store, parts, query):
    resource, *tail = parts
    with store.connect() as db:
        if resource == "images":
            if not tail:
                return list_images(db)
            if len(tail) == 1:
                return image_record(get_image(db, tail[0]))
            if len(tail) == 2 and tail[1] == "rows":
                return table_page(db, tail[0], query.get("table"), unsigned(query.get("offset", "0"), "offset"))
        if resource == "plans":
            if not tail:
                return list_plans(db)
            if len(tail) == 1:
                return read_plan(db, tail[0])
            if len(tail) == 2 and tail[1] == "attempts":
                pair = requested_pair(query)
                if pair != (None, None):
                    return _compare(db, tail[0], query, *pair)
                return list_attempts(db, tail[0])
            if len(tail) == 2 and tail[1] in {"compare", "diff"}:
                return _compare(db, tail[0], query)
            if len(tail) == 3 and tail[1] in {"attempts", "history"} and tail[2] in {"compare", "diff"}:
                return _compare(db, tail[0], query)
            if len(tail) == 4 and tail[1] in {"compare", "diff"}:
                return _compare(db, tail[0], query,
                                unsigned(tail[2], "a"), unsigned(tail[3], "b"))
            if len(tail) == 4 and tail[1] == "attempts" and tail[2].isdigit() and tail[3].isdigit():
                return _compare(db, tail[0], query, int(tail[2]), int(tail[3]))
            if len(tail) == 2 and tail[1] in {"download", "certificate"}:
                release = get_release(db, tail[0], body=tail[1] == "download")
                return release["candidate"] if tail[1] == "download" else json.loads(release["certificate"])
    raise Problem("route", "找不到该入口。", 404)


def upload(store, handler):
    if handler.headers.get_content_type() != "application/octet-stream":
        raise Problem("media_type", "请提交 application/octet-stream 格式的镜像。", 415)
    label = unquote(handler.headers.get("X-Image-Name", "")).strip() or "SQLite 镜像"
    return register(store, payload(handler, MAX_BYTES), label[:100])


def write_plan(store, handler, tail):
    if not tail:
        data = fields(handler, 32_768)
        return create_plan(store, {role: token(data.get(role), role) for role in ROLES})
    if len(tail) != 2:
        raise Problem("route", "找不到该入口。", 404)
    plan_id, action = tail
    data = fields(handler, 32_768)
    if action in {"compare", "diff"}:
        a, b = requested_pair(data)
        if a is None or b is None:
            raise Problem("compare_revisions", "请提供要比较的两版重演记录（a 与 b）。", 400)
        with store.connect() as db:
            return compare_attempts(db, plan_id, a, b, verify=wants_verify(data))
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
