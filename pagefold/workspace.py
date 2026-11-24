from .attempts import list_attempts, rehearse
from .demo import identity_base, image, type_shifted, vacuum_copy
from .errors import Problem
from .identity import compare_dossier
from .image_views import (compare_page, image_links, image_options, image_page,
                          rows_page)
from .images import (get_image, image_dossier, image_record, list_images,
                     register, table_page)
from .inputs import unsigned
from .merge import ROLES
from .plan_views import identity_strip, plan_links, plan_page
from .plans import create_plan, list_plans, read_plan
from .releases import seal
from .render import HERE, ROLE_LABELS, page, safe


def workspace(store, parts, query):
    with store.connect() as db:
        if not parts:
            images = list_images(db)
            return page("home", "三方合流工作区", choices=image_options(images),
                        images=image_links(images) or '<li class="muted">尚未登记镜像。</li>',
                        plans=plan_links(list_plans(db)) or '<p class="muted">尚未创建合流计划。</p>')
        if len(parts) == 2 and parts[0] == "plans":
            plan = read_plan(db, parts[1])
            images = [get_image(db, plan["sources"][role]) for role in ROLES]
            attempts = list_attempts(db, parts[1])
            published = db.execute("SELECT 1 FROM releases WHERE plan_id=?", (parts[1],)).fetchone()
            pairs = []
            for role_a, role_b in (("base", "left"), ("base", "right"), ("left", "right")):
                id_a, id_b = plan["sources"][role_a], plan["sources"][role_b]
                pairs.append((ROLE_LABELS[role_a], ROLE_LABELS[role_b], id_a, id_b,
                              compare_dossier(db, id_a, id_b)))
            return plan_page(plan, images, attempts, bool(published), identity_strip(pairs))
        if parts and parts[0] == "images" and len(parts) == 2 and parts[1] == "compare":
            left, right = query.get("a") or query.get("left"), query.get("b") or query.get("right")
            if not left or not right:
                raise Problem("compare_params", "请提供 a、b 两个镜像摘要。", 400)
            return compare_page(compare_dossier(db, left, right))
        if len(parts) in {2, 3} and parts[0] == "images":
            entry = image_record(get_image(db, parts[1]))
            if len(parts) == 2:
                return image_page(image_dossier(db, parts[1]))
            if parts[2] == "rows":
                return rows_page(entry, table_page(db, parts[1], query.get("table"),
                                                   unsigned(query.get("offset", "0"), "offset")))
    raise Problem("route", "找不到该入口。", 404)


def sample(store):
    entries = {role: register(store, image(flavor), label)["id"] for role, flavor, label in zip(
        ROLES, ("quartz", "cerulean", "umber"), ("石英祖本", "青蓝分支", "赤褐分支"))}
    return create_plan(store, entries)


def identity_sample(store):
    """布置三种展示：先封存使用旧原件的计划，再登记 VACUUM 等价件与类型漂移件。"""
    old = register(store, identity_base(), "身份样本·原件")
    solo = create_plan(store, {role: old["id"] for role in ROLES})
    outcome = rehearse(store, solo["id"], 0)
    if outcome.get("status") == "passed":
        seal(store, solo["id"], 0)
    vacuum = register(store, vacuum_copy(identity_base()), "身份样本·VACUUM 碎片件")
    shifted = register(store, type_shifted(), "身份样本·类型漂移件")
    mixed = create_plan(store, {"base": old["id"], "left": vacuum["id"], "right": shifted["id"]})
    return mixed


def error(problem, path):
    return page("error", "操作未完成", detail=safe(problem.detail),
                link=safe(path if path.startswith("/plans/") and not path.startswith("//") else "/"))


def asset(path):
    files = {"/assets/workspace.css": "text/css", "/assets/upload.js": "text/javascript"}
    if path not in files:
        raise Problem("route", "找不到该资源。", 404)
    return (HERE / path.lstrip("/")).read_bytes(), files[path]
