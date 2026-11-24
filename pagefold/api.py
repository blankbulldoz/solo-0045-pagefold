from urllib.parse import unquote

from .archive import archive
from .attempts import list_attempts, rehearse
from .decisions import resolve
from .errors import Problem
from .handovers import get_handover, issue
from .images import get_image, image_record, list_images, register, table_page
from .inputs import fields, payload, token, unsigned
from .merge import ROLES
from .plans import create_plan, list_plans, read_plan
from .releases import get_release, seal
from .sqlite_image import MAX_BYTES


def read(store, parts, query, keyring=None):
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
            if len(tail) == 2 and tail[1] in {"download", "certificate", "handover"}:
                action = tail[1]
                if action == "download":
                    return get_release(db, tail[0], body=True)["candidate"]
                if action == "certificate":
                    # 直接返回落库的规范字节，保证摘要可被收件方离线复现。
                    return get_release(db, tail[0])["certificate"].encode("ascii")
                # handover：get_handover 在缺失时抛出 no_handover；
                # 直接返回落库的规范字节，保证摘要可被收件方离线复现。
                get_handover(db, tail[0])
                return db.execute(
                    "SELECT document FROM handovers WHERE plan_id=?", (tail[0],)
                ).fetchone()["document"].encode("ascii")
        if resource == "keyring" and not tail and keyring is not None:
            return keyring.public_ring()
    raise Problem("route", "找不到该入口。", 404)


def upload(store, handler):
    if handler.headers.get_content_type() != "application/octet-stream":
        raise Problem("media_type", "请提交 application/octet-stream 格式的镜像。", 415)
    label = unquote(handler.headers.get("X-Image-Name", "")).strip() or "SQLite 镜像"
    return register(store, payload(handler, MAX_BYTES), label[:100])


def write_plan(store, handler, tail, keyring=None):
    if not tail:
        data = fields(handler, 32_768)
        return create_plan(store, {role: token(data.get(role), role) for role in ROLES})
    if len(tail) != 2:
        raise Problem("route", "找不到该入口。", 404)
    plan_id, action = tail
    data = fields(handler, 32_768)
    if action == "handover":
        if keyring is None:
            raise Problem("keyring", "服务未配置组织密钥环，无法颁发交接凭证。", 503)
        return issue(store, keyring, plan_id)
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
