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
from .rules import create_rule, get_rule, list_rules
from .suggestions import trial_plan
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
        if resource == "rules":
            if not tail:
                return list_rules(db)
            if len(tail) == 1:
                return get_rule(db, tail[0])
        if resource == "plans":
            if not tail:
                return list_plans(db)
            if len(tail) == 1:
                return read_plan(db, tail[0])
            if len(tail) == 2 and tail[1] == "suggestions":
                return trial_plan(db, tail[0])
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


def create_rule_request(store, handler):
    data = fields(handler, 32_768)
    spec = data.get("rule")
    if spec is None:
        spec = data.get("spec", data)
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError as exc:
            raise Problem("invalid_rule", "rule JSON 无法解析。", 400) from exc
    return create_rule(store, spec)


def trial_rule_request(store, handler):
    data = fields(handler, 32_768)
    plan_id = data.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id:
        raise Problem("plan_id", "请填写要试算的计划 ID。", 400)
    spec = data.get("rule")
    if spec is None:
        spec = data.get("spec")
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError as exc:
            raise Problem("invalid_rule", "rule JSON 无法解析。", 400) from exc
    if not isinstance(spec, dict):
        raise Problem("invalid_rule", "请提供规则 JSON 对象。", 400)
    with store.connect() as db:
        return trial_plan(db, plan_id, [spec], include_stored=True)


def write_plan(store, handler, tail):
    if not tail:
        data = fields(handler, 32_768)
        return create_plan(store, {role: token(data.get(role), role) for role in ROLES})
    if len(tail) != 2:
        raise Problem("route", "找不到该入口。", 404)
    plan_id, action = tail
    data = fields(handler, 32_768)
    if action == "suggestions":
        drafts = data.get("rules")
        if drafts is not None:
            pass
        elif isinstance(data.get("rule"), dict):
            drafts = [data["rule"]]
        elif "choice" in data and any(key in data for key in ("when", "match", "conditions")):
            drafts = [data]
        else:
            drafts = []
        if not isinstance(drafts, list) or not all(isinstance(rule, dict) for rule in drafts):
            raise Problem("invalid_rule", "rules 须为规则对象数组。", 400)
        include_stored = data.get("include_stored", True)
        if not isinstance(include_stored, bool):
            raise Problem("invalid_rule", "include_stored 须为布尔值。", 400)
        with store.connect() as db:
            return trial_plan(db, plan_id, drafts, include_stored)
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
