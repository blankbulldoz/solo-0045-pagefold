import json
import re
from urllib.parse import unquote

from .archive import archive
from .attempts import list_attempts, rehearse
from .decisions import resolve
from .errors import Problem
from .images import get_image, image_record, list_images, register, table_page
from .inputs import fields, payload, token, unsigned
from .merge import ROLES
from .plans import create_plan, list_plans, read_plan
from .releases import get_release, seal, seal_preview
from .sqlite_image import MAX_BYTES

TRUTHY = {"yes", "true", "1", "on", "confirmed", "confirm"}
HEX64 = re.compile(r"[a-f0-9]{64}\Z")


def truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value == 1:
        return True
    return isinstance(value, str) and value.strip().lower() in TRUTHY


def _json_object(value, field):
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise Problem("body", f"字段 {field} 须为 JSON 对象。", 400) from exc
        if not isinstance(parsed, dict):
            raise Problem("body", f"字段 {field} 须为 JSON 对象。", 400)
        return parsed
    raise Problem("body", f"字段 {field} 须为对象。", 400)


def read_confirmation(data, revision):
    """Normalise the explicit seal confirmation from JSON or HTML form fields.

    Returns (acknowledged, confirmation); confirmation stays None when the
    caller supplied no binding material at all. Flat hidden fields are binding
    material, but only an explicit confirmed/confirm flag acknowledges them:
    an unchecked HTML confirmation box omits that flag.
    """
    acknowledged = truthy(data.get("confirmed")) or truthy(data.get("confirm")) \
        or truthy(data.get("acknowledged")) or truthy(data.get("ack"))
    confirmation = {}
    material = False
    nested = _json_object(data.get("confirmation"), "confirmation")
    if nested is not None:
        material = True
        # A machine client that hands back the evidence object confirms explicitly.
        acknowledged = True
        confirmation.update(nested)

    def pick(*names):
        for name in names:
            value = data.get(name)
            if value is not None and value != "":
                return value
        return None

    claimed_plan = pick("confirmation_plan", "confirm_plan", "plan")
    if claimed_plan is not None:
        if not isinstance(claimed_plan, str) or not claimed_plan.strip():
            raise Problem("body", "confirmation_plan 须为计划编号。", 400)
        material = True
        confirmation["plan"] = claimed_plan.strip()
    explicit_revision = pick("confirmation_revision", "confirm_revision")
    if explicit_revision is not None:
        material = True
        confirmation["revision"] = unsigned(explicit_revision, "confirmation_revision")
    digest = pick("confirmation_digest", "confirm_digest", "candidate_digest", "digest")
    if digest is not None:
        if not isinstance(digest, str) or not HEX64.fullmatch(digest):
            raise Problem("token", "confirmation_digest 须为 64 位十六进制摘要。", 400)
        material = True
        confirmation["candidate_digest"] = digest
    sources_value = pick("sources", "confirmation_sources")
    sources = _json_object(sources_value, "sources") if sources_value is not None else None
    if sources is None:
        flat = {role: pick(f"source_{role}", f"confirm_{role}") for role in ROLES}
        if any(value is not None for value in flat.values()):
            sources = flat
    if sources is not None:
        material = True
        confirmation["sources"] = sources
    if not material:
        return acknowledged, None
    confirmation.setdefault("revision", revision)
    if confirmation.get("revision") is not None:
        confirmation["revision"] = unsigned(confirmation["revision"], "confirmation_revision")
    return acknowledged, confirmation


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
            if len(tail) == 2 and tail[1] in {"seal", "confirmation", "seal-preview", "evidence"}:
                return seal_preview(db, tail[0])
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
    if action == "seal":
        # Seal may carry revision at top level or inside the confirmation object.
        raw_revision = data.get("revision")
        top_revision = None if raw_revision in (None, "") else unsigned(raw_revision, "revision")
        acknowledged, confirmation = read_confirmation(data, top_revision)
        revision = top_revision
        if revision is None and confirmation is not None:
            revision = confirmation.get("revision")
        if not isinstance(revision, int):
            raise Problem("number", "字段 revision 须为非负整数。", 400)
        return seal(store, plan_id, revision, acknowledged=acknowledged,
                    confirmation=confirmation)
    revision = unsigned(data.get("revision"), "revision")
    if action == "decisions":
        choice = data.get("choice")
        if choice not in (*ROLES, "clear"):
            raise Problem("choice", "请选择祖本、左支、右支或清除裁决。", 400)
        return resolve(store, plan_id, revision, token(data.get("item"), "item"), choice)
    writes = {"rehearse": rehearse, "archive": archive}
    if action in writes:
        return writes[action](store, plan_id, revision)
    raise Problem("route", "找不到该入口。", 404)
