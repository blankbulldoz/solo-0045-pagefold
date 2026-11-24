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
from .port_attempts import list_port_attempts, rehearse_port
from .port_plans import PORT_CHOICES, create_port, decide_port, list_ports, read_port
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
        if resource == "ports":
            if not tail:
                return list_ports(db)
            if len(tail) == 1:
                return read_port(db, tail[0])
            if len(tail) == 2 and tail[1] == "attempts":
                return list_port_attempts(db, tail[0])
            if len(tail) == 2 and tail[1] == "download":
                attempt = latest_port_candidate(db, tail[0])
                return attempt["candidate"]
    raise Problem("route", "找不到该入口。", 404)


def latest_port_candidate(db, port_id):
    row = db.execute(
        "SELECT candidate FROM port_attempts WHERE port_id=? AND candidate IS NOT NULL "
        "ORDER BY revision DESC LIMIT 1", (port_id,)).fetchone()
    if row is None:
        raise Problem("unpublished", "该移植还没有通过重演的候选。", 404)
    return row


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


def write_port(store, handler, tail):
    if not tail:
        data = fields(handler, 32_768)
        return create_port(store, {role: token(data.get(role), role) for role in ROLES})
    if len(tail) != 2:
        raise Problem("route", "找不到该入口。", 404)
    port_id, action = tail
    data = fields(handler, 32_768)
    revision = unsigned(data.get("revision"), "revision")
    if action == "decisions":
        choice = data.get("choice")
        if choice not in PORT_CHOICES:
            raise Problem("choice", "请选择上轮祖本、改动分支、新祖本或清除裁决。", 400)
        return decide_port(store, port_id, revision, token(data.get("item"), "item"), choice)
    if action == "rehearse":
        return rehearse_port(store, port_id, revision)
    raise Problem("route", "找不到该入口。", 404)
