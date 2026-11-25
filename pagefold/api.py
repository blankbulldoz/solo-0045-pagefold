import json
from urllib.parse import unquote

from .archive import archive
from .attempts import list_attempts, rehearse
from .decisions import resolve_decision
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
                plan = read_plan(db, tail[0])
                return plan
            if len(tail) == 2 and tail[1] in {"entities", "entity", "entity-keys", "renames", "rename", "groups"}:
                plan = read_plan(db, tail[0])
                if tail[1] in {"entity-keys", "entity-key"}:
                    return plan["entity_keys"]
                return {"entities": plan["entities"], "renames": plan["renames"]}
            if len(tail) == 3 and tail[1] in {"entities", "entity", "entity-decisions",
                                               "renames", "rename", "groups"}:
                plan = read_plan(db, tail[0])
                entity = next((item for item in plan["entities"] if item["id"] == tail[2]), None)
                if entity is None:
                    raise Problem("unknown_entity", "找不到指定的实体改名组。", 404)
                return entity
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
        sources = {role: token(data.get(role), role) for role in ROLES}
        return create_plan(store, sources, data)
    plan_id = tail[0]
    data = fields(handler, 32_768)
    if len(tail) >= 3 and tail[1] in {"entities", "entity", "entity-decisions",
                                      "renames", "rename", "groups"}:
        entity_id = tail[2]
        if len(tail) == 4 and tail[3] in {"accept", "accepted", "reject", "rejected",
                                          "left", "right", "base", "clear"}:
            data = dict(data)
            data.setdefault("choice", tail[3])
        elif len(tail) > 4 or (len(tail) == 4 and tail[3] not in {"decision", "decisions"}):
            raise Problem("route", "找不到该实体裁决入口。", 404)
        data = dict(data)
        data["entity"] = entity_id
        revision = unsigned(data.get("revision"), "revision")
        return resolve_decision(store, plan_id, revision, data)
    if len(tail) != 2:
        raise Problem("route", "找不到该入口。", 404)
    action = tail[1]
    revision = unsigned(data.get("revision"), "revision")
    if action in {"decisions", "entities", "entity", "entity-decisions",
                  "entity-decision", "renames", "rename"}:
        if action != "decisions":
            data = dict(data)
            data.setdefault("entity", data.get("entity") or data.get("item") or data.get("rename"))
        return resolve_decision(store, plan_id, revision, data)
    writes = {"rehearse": rehearse, "seal": seal, "archive": archive}
    if action in writes:
        return writes[action](store, plan_id, revision)
    raise Problem("route", "找不到该入口。", 404)
