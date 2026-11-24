from .attempts import list_attempts
from .demo import image
from .errors import Problem
from .image_views import image_links, image_options, image_page, rows_page
from .images import get_image, image_record, list_images, register, table_page
from .inputs import unsigned
from .merge import ROLES
from .plan_views import plan_links, plan_page
from .plans import create_plan, list_plans, read_plan
from .render import HERE, option, page, safe


def source_panel(sources):
    if sources is None or not sources.enabled:
        return ""
    named = sources.describe()["sources"]
    if named:
        source_input = ('<select name="source" required>' +
                        "".join(option(item["name"], f'{item["label"]} · {item["name"]}')
                                for item in named) + "</select>")
    elif sources.roots:
        source_input = '<input name="source" required placeholder="允许目录中的 SQLite 文件名">'
    else:
        return ""
    return """
<section class="paper source-collect">
  <div class="panel-title"><h2>从在线 SQLite 采集时点</h2><span class="tag">服务端允许清单</span></div>
  <form class="upload" method="post" action="/sources/collect">
    <label><span class="input-title">允许的本机源</span>
      """ + source_input + """
    </label>
    <label><span class="input-title">登记标签（可选）</span>
      <input name="label" maxlength="100" placeholder="例如：WAL 早高峰时点">
    </label>
    <button class="primary" type="submit">一致读取并登记</button>
    <p>使用 SQLite Backup API 获取单一一致点；包含提交以镜像内提交凭证为准，原库以只读方式打开。</p>
  </form>
</section>
"""


def workspace(store, parts, query, sources=None):
    with store.connect() as db:
        if not parts:
            images = list_images(db)
            return page("home", "三方合流工作区", choices=image_options(images),
                        images=image_links(images) or '<li class="muted">尚未登记镜像。</li>',
                        plans=plan_links(list_plans(db)) or '<p class="muted">尚未创建合流计划。</p>',
                        source_panel=source_panel(sources))
        if len(parts) == 2 and parts[0] == "plans":
            plan = read_plan(db, parts[1])
            images = [get_image(db, plan["sources"][role]) for role in ROLES]
            attempts = list_attempts(db, parts[1])
            published = db.execute("SELECT 1 FROM releases WHERE plan_id=?", (parts[1],)).fetchone()
            return plan_page(plan, images, attempts, bool(published))
        if len(parts) in {2, 3} and parts[0] == "images":
            entry = image_record(get_image(db, parts[1]))
            if len(parts) == 2:
                return image_page(entry)
            if parts[2] == "rows":
                return rows_page(entry, table_page(db, parts[1], query.get("table"),
                                                   unsigned(query.get("offset", "0"), "offset")))
    raise Problem("route", "找不到该入口。", 404)


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
