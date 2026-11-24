from .render import ROLE_LABELS, STATE_LABELS, fragment, page, safe, typed
from .review import run_review, sealed_plan

LABELS = {**ROLE_LABELS, "result": "成果"}
VERDICTS = {"equal": ("结果一致", "auto"), "different": ("结果不同", "conflict"),
            "incomparable": ("不可比较", "")}


def role_panel(role):
    label = LABELS[role["role"]]
    if role["status"] != "ok":
        detail = (f'<dl class="meta"><dt>来源摘要</dt><dd><code>{role["source"]}</code></dd></dl>'
                  f'<p class="review-error">{safe(role["error"]["detail"])} '
                  f'<small>{safe(role["error"]["code"])}</small></p>')
        return fragment("review_role", label=label, badge="", status="无法执行", detail=detail)
    columns = "".join(f'<th scope="col">{safe(column)}</th>' for column in role["columns"])
    rows = "".join("<tr>" + f"<td>{index}</td>" + "".join(f"<td>{typed(cell)}" for cell in row) + "</tr>"
                   for index, row in enumerate(role["rows"], 1))
    detail = (f'<dl class="meta"><dt>来源摘要</dt><dd><code>{role["source"]}</code></dd>'
              f'<dt>结果摘要</dt><dd><code>{role["digest"]}</code></dd>'
              f'<dt>行数</dt><dd>{role["row_count"]}</dd></dl>'
              f'<div class="scroll"><table><thead><tr><th scope="col">#</th>{columns}</tr></thead>'
              f"<tbody>{rows}</tbody></table></div>")
    return fragment("review_role", label=label, badge="auto", status="已执行", detail=detail)


def comparisons(evidence):
    blocks = []
    for item in evidence["comparisons"]:
        first, second = item["pair"]
        text, badge = VERDICTS[item["verdict"]]
        blocks.append(f'<div class="attempt"><span>{LABELS[first]} ↔ {LABELS[second]}</span>'
                      f'<span class="tag {badge}">{text}</span></div>')
    return "".join(blocks)


def review_page(db, plan_id, sql):
    plan = sealed_plan(db, plan_id)
    report = ""
    if sql.strip():
        evidence = run_review(db, plan_id, sql)
        report = fragment("review_report", sql_digest=evidence["sql_digest"][:16],
                          comparisons=comparisons(evidence),
                          roles="".join(role_panel(role) for role in evidence["roles"]))
    return page("review", "封存查询复核", plan=plan_id, short=plan_id[:12],
                state=STATE_LABELS[plan["state"]], sql=safe(sql), report=report)
