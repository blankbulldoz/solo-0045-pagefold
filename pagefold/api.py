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
from .previews import build_preview, confirm_preview, read_preview, valid_token
from .releases import get_release, seal
from .sqlite_image import MAX_BYTES

TOKEN_FIELDS = ("preview_token", "token", "confirmation_token", "confirm_token")
PREVIEW_MODE_VALUES = {"1", "true", "yes", "preview", "confirm-first"}


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
            if len(tail) == 2 and tail[0] == "preview":
                return read_preview(db, tail[1])
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


def source_fields(data, required=True):
    if required:
        return {role: token(data.get(role), role) for role in ROLES}
    present = [role for role in ROLES if data.get(role) not in (None, "")]
    if not present:
        return None
    if len(present) != len(ROLES):
        raise Problem("sources", "必须同时给出 base、left、right 三个来源摘要。", 400)
    return {role: token(data[role], role) for role in ROLES}


def preview_token_field(data):
    for name in TOKEN_FIELDS:
        value = data.get(name)
        if value not in (None, ""):
            if not valid_token(value):
                raise Problem("preview_token", "preview_token 须为 64 位十六进制预览凭证。", 400)
            return value
    # The "preview" field may carry a token string or act as a mode flag.
    value = data.get("preview")
    if valid_token(value):
        return value
    if isinstance(value, str) and value.strip() and not preview_mode(value):
        raise Problem("preview_token", "preview 凭证须为 64 位十六进制字符串。", 400)
    return None


def preview_mode(value):
    return value is True or (isinstance(value, str) and value.strip().lower() in PREVIEW_MODE_VALUES)


def write_plan(store, handler, tail):
    if not tail:
        data = fields(handler, 32_768)
        preview = preview_token_field(data)
        if preview:
            # Sources are optional here; when present they must match the
            # server-side preview binding or the confirmation is rejected.
            return confirm_preview(store, preview, source_fields(data, required=False))
        sources = source_fields(data)
        if preview_mode(data.get("preview")):
            return build_preview(store, sources)
        return create_plan(store, sources)
    data = fields(handler, 32_768)
    if tail in (["preview"], ["prepare"]):
        return build_preview(store, source_fields(data))
    if len(tail) == 2 and tail[0] == "preview":
        # POST /api/plans/preview/{token} confirms that preview.
        return confirm_preview(store, tail[1], source_fields(data, required=False))
    if len(tail) == 3 and tail[0] == "preview" and tail[2] == "confirm":
        return confirm_preview(store, tail[1], source_fields(data, required=False))
    if len(tail) != 2:
        raise Problem("route", "找不到该入口。", 404)
    plan_id, action = tail
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
