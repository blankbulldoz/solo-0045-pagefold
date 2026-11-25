import json
from urllib.parse import unquote

from .archive import archive
from .attempts import list_attempts, rehearse
from .decisions import resolve
from .errors import Problem
from .groups import group_entries, list_groups, read_group, register_group
from .images import get_image, image_record, list_images, register, table_page
from .inputs import fields, payload, token, unsigned
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
        if resource == "groups":
            if not tail:
                return list_groups(db)
            if len(tail) == 1:
                return read_group(db, tail[0])
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
    nature = handler.headers.get_content_type()
    if nature == "application/octet-stream":
        label = unquote(handler.headers.get("X-Image-Name", "")).strip() or "SQLite 镜像"
        return register(store, payload(handler, MAX_BYTES), label[:100])
    # 同一登记入口承接带角色的三方组提交（multipart 表单或 JSON）。
    if nature == "application/json" or (nature and nature.startswith("multipart/form-data")):
        return register_group(store, group_entries(handler))
    raise Problem("media_type", "请提交 application/octet-stream 镜像或带角色的镜像组。", 415)


def write_group(store, handler):
    """网页表单入口：浏览器只能以 multipart 提交三份带角色的文件。"""
    if not (handler.headers.get_content_type() or "").startswith("multipart/form-data"):
        raise Problem("media_type", "网页组登记请使用 multipart 表单。", 415)
    return register_group(store, group_entries(handler))


def write_plan(store, handler, tail):
    if not tail:
        data = fields(handler, 32_768)
        sources = _plan_sources(store, data)
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


def _plan_sources(store, data):
    # 计划可直接承接一次镜像组登记，组内角色映射固定，避免手工重选错配。
    group_ref = data.get("group")
    if group_ref:
        gid = token(group_ref, "group")
        with store.connect() as db:
            return read_group(db, gid)["sources"]
    return {role: token(data.get(role), role) for role in ROLES}
