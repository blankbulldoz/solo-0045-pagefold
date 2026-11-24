from .attempts import list_attempts
from .demo import image
from .errors import Problem
from .image_views import image_links, image_options, image_page, rows_page
from .images import get_image, image_record, list_images, register, table_page
from .inputs import unsigned
from .merge import ROLES
from .plan_views import plan_links, plan_page
from .plans import create_plan, list_plans, read_plan
from .render import HERE, page, safe
from .witness import witness_graph
from .witness_views import WITNESS_TAIL, witness_entry_page, witness_result_page


def _image_choices(db):
    return [image_record(get_image(db, row["id"])) for row in list_images(db)]


WITNESS_ENTRY_PATHS = {"witness", "references", "refs", "neighbors", "graph",
                       "lineage", "related", "trace"}


def workspace(store, parts, query):
    with store.connect() as db:
        if not parts:
            images = list_images(db)
            return page("home", "三方合流工作区", choices=image_options(images),
                        images=image_links(images) or '<li class="muted">尚未登记镜像。</li>',
                        plans=plan_links(list_plans(db)) or '<p class="muted">尚未创建合流计划。</p>')
        if parts[0] in WITNESS_ENTRY_PATHS and len(parts) <= 3:
            # 兼容 /witness、/witness/{image_id}、/witness/{image_id}/{table}。
            effective = dict(query)
            if len(parts) >= 2:
                effective.setdefault("image", parts[1])
            if len(parts) >= 3:
                effective.setdefault("table", parts[2])
            choices = _image_choices(db)
            if (effective.get("step") != "columns" and effective.get("image")
                    and effective.get("table") and _has_key(effective)):
                result = witness_graph(db, query=effective)
                return witness_result_page(result, choices)
            return witness_entry_page(choices, effective)
        if len(parts) == 2 and parts[0] == "plans":
            plan = read_plan(db, parts[1])
            images = [get_image(db, plan["sources"][role]) for role in ROLES]
            attempts = list_attempts(db, parts[1])
            published = db.execute("SELECT 1 FROM releases WHERE plan_id=?", (parts[1],)).fetchone()
            return plan_page(plan, images, attempts, bool(published))
        if len(parts) >= 2 and parts[0] == "images":
            entry = image_record(get_image(db, parts[1]))
            if len(parts) == 2:
                return image_page(entry)
            if parts[2] == "rows" and len(parts) == 3:
                return rows_page(entry, table_page(db, parts[1], query.get("table"),
                                                   unsigned(query.get("offset", "0"), "offset")))
            if parts[2] in WITNESS_TAIL and len(parts) <= 4:
                effective = dict(query)
                if len(parts) == 4:
                    effective.setdefault("table", parts[3])
                choices = _image_choices(db)
                result = witness_graph(db, parts[1], effective.get("table"),
                                       effective.get("key"), query=effective)
                return witness_result_page(result, choices)
    raise Problem("route", "找不到该入口。", 404)


def _has_key(query):
    if query.get("key"):
        return True
    if any(key.startswith("v_") for key in query):
        return True
    return False


def sample(store):
    entries = {role: register(store, image(flavor), label)["id"] for role, flavor, label in zip(
        ROLES, ("quartz", "cerulean", "umber"), ("石英祖本", "青蓝分支", "赤褐分支"))}
    return create_plan(store, entries)


def error(problem, path):
    return page("error", "操作未完成", detail=safe(problem.detail),
                link=safe(path if path.startswith("/plans/") and not path.startswith("//") else "/"))


def asset(path):
    files = {"/assets/workspace.css": "text/css", "/assets/upload.js": "text/javascript"}
    if path not in files:
        raise Problem("route", "找不到该资源。", 404)
    return (HERE / path.lstrip("/")).read_bytes(), files[path]
