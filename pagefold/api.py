import json
from urllib.parse import unquote

from . import packets
from .archive import archive
from .attempts import list_attempts, rehearse
from .decisions import history, resolve
from .errors import Problem
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
        if resource == "plans":
            if not tail:
                return list_plans(db)
            if len(tail) == 1:
                return read_plan(db, tail[0])
            if len(tail) == 2 and tail[1] == "attempts":
                return list_attempts(db, tail[0])
            if len(tail) == 2 and tail[1] == "decisions":
                return history(db, tail[0])
            if len(tail) == 2 and tail[1] == "packets":
                return packets.list_packets(db, tail[0])
            if len(tail) == 2 and tail[1] in {"download", "certificate"}:
                release = get_release(db, tail[0], body=tail[1] == "download")
                return release["candidate"] if tail[1] == "download" else json.loads(release["certificate"])
        if resource == "packets" and len(tail) == 1:
            return packets.get_packet(db, tail[0])
        if resource == "packets" and len(tail) == 2 and tail[1] == "export":
            return packets.get_packet(db, tail[0])["export"]
    raise Problem("route", "找不到该入口。", 404)


def upload(store, handler):
    if handler.headers.get_content_type() != "application/octet-stream":
        raise Problem("media_type", "请提交 application/octet-stream 格式的镜像。", 415)
    label = unquote(handler.headers.get("X-Image-Name", "")).strip() or "SQLite 镜像"
    return register(store, payload(handler, MAX_BYTES), label[:100])


def _items(data):
    raw = data.get("items")
    if raw is None:
        return [] if data.get("items_mode") == "selected" else None
    values = raw if isinstance(raw, list) else [raw]
    return [value for value in values if value]


def write_plan(store, handler, tail):
    if not tail:
        data = fields(handler, 32_768)
        return create_plan(store, {role: token(data.get(role), role) for role in ROLES})
    if len(tail) != 2:
        raise Problem("route", "找不到该入口。", 404)
    plan_id, action = tail
    data = fields(handler, 65_536)
    if action == "packets":
        return packets.issue(store, plan_id, unsigned(data.get("revision"), "revision"),
                             data.get("recipient"), _items(data))
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


def _selections(data):
    raw = data.get("selections")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise Problem("selection", "请提交行选择对象。", 400) from exc
    if raw is not None:
        return raw
    # 网页表单：s_<行摘要>=最终选择；s_<行摘要>_steps=按顺序的步骤，如「clear,left」。
    selections = {key[2:]: value for key, value in data.items()
                  if key.startswith("s_") and not key.endswith("_steps") and value}
    for key, value in data.items():
        if key.startswith("s_") and key.endswith("_steps"):
            steps = [step for step in (value if isinstance(value, list) else value.split(",")) if step]
            if steps:
                selections[key[2:-len("_steps")]] = steps
    return selections


def write_packet(store, handler, tail):
    if len(tail) != 2:
        raise Problem("route", "找不到该入口。", 404)
    packet_id, action = tail
    data = fields(handler, 65_536)
    if action == "return":
        return packets.accept_return(store, packet_id, token(data.get("digest"), "digest"),
                                     _selections(data), data.get("note"))
    revision = unsigned(data.get("revision"), "revision")
    if action == "merge":
        return packets.merge(store, packet_id, revision)
    if action == "withdraw":
        return packets.withdraw(store, packet_id, revision)
    raise Problem("route", "找不到该入口。", 404)
