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
from .sources import SourcePolicy, collect_source, get_collection, list_collections
from .sqlite_image import MAX_BYTES


def read(store, parts, query, sources=None):
    resource, *tail = parts
    with store.connect() as db:
        policy = sources or SourcePolicy()
        if resource == "sources":
            if not tail:
                return policy.describe()
            if len(tail) == 1:
                return policy.resolve(tail[0]).public()
        if resource == "collections":
            if not tail:
                return list_collections(db)
            if len(tail) == 1:
                return get_collection(db, tail[0])
        if resource == "images":
            if not tail:
                return list_images(db)
            if len(tail) == 1:
                return image_record(get_image(db, tail[0]))
            if len(tail) == 2 and tail[1] in {"download", "raw"}:
                return get_image(db, tail[0], body=True)["body"]
            if len(tail) == 2 and tail[1] == "rows":
                return table_page(db, tail[0], query.get("table"), unsigned(query.get("offset", "0"), "offset"))
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


def optional_fields(handler):
    lengths = handler.headers.get_all("Content-Length", [])
    if not lengths or lengths == ["0"]:
        return {}
    return fields(handler, 32_768)


def collect(store, policy, handler, selector=None):
    data = optional_fields(handler)
    if selector is None:
        selector = data.get("source") or data.get("name") or data.get("path")
    label = data.get("label")
    if label is not None and not isinstance(label, str):
        raise Problem("body", "字段 label 须为字符串。", 400)
    return collect_source(store, policy, selector, label=label)


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
