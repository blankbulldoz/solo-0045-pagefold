import json

from .render import ROLE_LABELS, page, safe
from .plan_views import CATEGORY_LABELS, suggestion_card


RULE_TEMPLATE = {
    "name": "新的选行习惯",
    "description": "说明这条规则命中的冲突类型",
    "category": "general",
    "enabled": True,
    "priority": 100,
    "choice": "right",
    "tables": ["habit_case"],
    "when": {
        "op": "all",
        "conditions": [
            {"op": "delete_vs_edit", "deleted_role": "left", "edited_role": "right"}
        ],
    },
}


def rule_card(rule):
    opinion = "交人工判断" if rule["choice"] == "manual" else ROLE_LABELS[rule["choice"]]
    spec = json.dumps(rule.get("when", {}), ensure_ascii=False, indent=2)
    tables = "、".join(rule.get("tables") or ["全部表"])
    state = "启用" if rule.get("enabled", True) else "停用"
    return f"""<article class="rule-card">
      <header><h3>{safe(rule['name'])}</h3>
        <span class="tag">{CATEGORY_LABELS.get(rule.get('category'), '一般')} · P{rule['priority']} · {state}</span>
      </header>
      <p>{safe(rule.get('description') or '没有说明')}</p>
      <p class="muted">表：{safe(tables)} · 意见：<b>{safe(opinion)}</b> · ID：<code>{safe(rule['id'])}</code></p>
      <pre class="rule-json">{safe(spec)}</pre>
    </article>"""


def rules_page(rules, plan_id=""):
    cards = "".join(rule_card(rule) for rule in rules) or '<p class="empty">还没有规则。</p>'
    template = json.dumps(RULE_TEMPLATE, ensure_ascii=False, indent=2)
    body = f"""
<a class="back" href="/">← 工作区</a>
<section class="heading"><div><h1>裁决建议规则</h1>
<p>规则只负责试算和说明命中证据，不会自动写裁决；人在计划页决定是否采纳。</p></div>
<a class="tag" href="/">返回计划</a></section>
<div class="rules-layout">
  <section class="paper"><div class="panel-title"><h2>已保存规则</h2><span class="tag">{len(rules)} 条</span></div>
    <div class="rule-cards">{cards}</div>
  </section>
  <aside class="paper rule-editor"><div class="panel-title"><h2>新建规则</h2></div>
    <form method="post" action="/rules" class="new-rule-form">
      <label><span class="input-title">规则 JSON</span>
      <span class="input-desc">choice 可为 base、left、right 或 manual；整行缺席用 row_state=absent，SQL NULL 用 column_state=sql_null。</span>
      <textarea name="spec" rows="22" spellcheck="false">{safe(template)}</textarea></label>
      <label><span class="input-title">试算计划 ID</span>
      <input name="plan_id" value="{safe(plan_id)}" placeholder="可从 /plans/... 地址复制"></label>
      <div class="form-actions">
        <button class="primary" type="submit" formaction="/rules">保存规则</button>
        <button type="submit" formaction="/rules/trial">不保存，先试算草稿</button>
      </div>
    </form>
    <div class="rule-api muted">
      <p>接口：<code>POST /api/rules</code>；计划试算：<code>GET /api/plans/&lt;id&gt;/suggestions</code>。</p>
      <p>未保存草稿可 POST 到 <code>/api/plans/&lt;id&gt;/suggestions</code>，body 为 <code>{{"rule": {{...}}}}</code>。</p>
    </div>
  </aside>
</div>"""
    return page("rules", "建议规则", body=body)


def draft_trial_page(trial):
    plan = {"id": trial["plan_id"], "revision": trial["revision"], "state": trial["state"]}
    cards = "".join(suggestion_card(plan, suggestion) for suggestion in trial["suggestions"])
    if not cards:
        cards = '<p class="empty">这个计划当前没有冲突项。</p>'
    summary = trial["summary"]
    body = f"""
<a class="back" href="/rules">← 返回规则</a>
<section class="heading"><div><h1>规则草稿试算</h1>
<p>草稿尚未保存，也没有写入任何裁决或推进版本；以下包含已保存规则与本次草稿的共同意见。</p></div>
<a class="tag" href="/plans/{trial['plan_id']}">打开计划</a></section>
<section class="paper suggestion-panel">
  <div class="panel-title"><h2>试算结果</h2>
  <span class="tag">{summary['conflicts']} 项冲突 · {summary['recommended']} 项可采纳 · {summary['contested']} 项重叠冲突</span></div>
  <div class="suggestion-list">{cards}</div>
</section>"""
    return page("rules", "规则草稿试算", body=body)
