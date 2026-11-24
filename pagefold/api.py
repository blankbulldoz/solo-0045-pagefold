import json
from urllib.parse import unquote

from .archive import archive
from .attempts import list_attempts, rehearse
from .decisions import resolve
from .errors import Problem
from .images import get_image, image_record, list_images, register, table_page
from .inputs import fields, payload, token, unsigned
from .manual import ROW_FIELDS, has_row_payload, normalize_choice_name
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
    data = fields(handler, 1_048_576, max_fields=5000)
    revision = unsigned(data.get("revision"), "revision")
    if action in ("decisions", "manual"):
        choice = data.get("choice")
        if isinstance(choice, list):
            # 兼容直接把行数组放在 choice 上的请求形态。
            data = dict(data, row=choice)
            choice = "manual"
        if isinstance(choice, dict):
            envelope = choice
            kind = envelope.get("choice", envelope.get("mode", envelope.get("type", "manual")))
            choice = normalize_choice_name(kind)
            if choice is None:
                raise Problem("choice", "请选择祖本、左支、右支、人工合成或清除裁决。", 400)
            data = dict(data)
            for field in ROW_FIELDS:
                if field in envelope and field not in data:
                    data[field] = envelope[field]
        else:
            normalized = normalize_choice_name(choice) if choice is not None else None
            if normalized is not None:
                choice = normalized
            elif choice is None and (action == "manual" or has_row_payload(data)):
                # 允许省略 choice，仅凭 row/manual 载荷表达人工合成。
                choice = "manual"
            else:
                raise Problem("choice", "请选择祖本、左支、右支、人工合成或清除裁决。", 400)
        item_ref = data.get("item", data.get("item_id"))
        if isinstance(item_ref, dict):
            if "table" not in item_ref or "key" not in item_ref:
                raise Problem("item", "item 对象须包含 table 与 key。", 400)
        else:
            item_ref = token(item_ref, "item")
        return resolve(store, plan_id, revision, item_ref, choice, data)
    writes = {"rehearse": rehearse, "seal": seal, "archive": archive}
    if action in writes:
        return writes[action](store, plan_id, revision)
    raise Problem("route", "找不到该入口。", 404)
