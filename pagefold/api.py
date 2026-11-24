import json
from urllib.parse import unquote

from . import lineage
from .archive import archive
from .attempts import list_attempts, rehearse
from .decisions import resolve
from .errors import Problem
from .images import get_image, image_record, list_images, register, table_page
from .inputs import fields, payload, token, token_list, unsigned
from .merge import ROLES
from .plans import create_plan, list_plans, read_plan
from .releases import get_release, seal
from .sqlite_image import MAX_BYTES


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
            if len(tail) == 2 and tail[1] == "lineage":
                return lineage.image_lineage(db, tail[0])
            if len(tail) == 3 and tail[1] == "lineage" and tail[2] == "routes":
                return lineage.derivation_routes(db, tail[0])
        if resource == "lineage" and len(tail) == 1 and tail[0] == "preflight":
            sources = {role: token(query.get(role), role) for role in ROLES if role in query}
            return lineage.shared_inputs(db, sources)
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
    claims = _header_tokens(handler.headers.get_all("X-Lineage-Claims"))
    missing = unquote(handler.headers.get("X-Lineage-Missing", "")).strip() or None
    return register(store, payload(handler, MAX_BYTES), label[:100], claims, missing)


def _header_tokens(values):
    if not values:
        return None
    merged = ",".join(values)
    return token_list(merged, "X-Lineage-Claims") if merged.strip() else None


def write_plan(store, handler, tail):
    if not tail:
        data = fields(handler, 32_768)
        sources = {role: token(data.get(role), role) for role in ROLES}
        if data.get("preflight"):
            return lineage.preflight(store, sources)
        return create_plan(store, sources)
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
