import json

from .packets import describe
from .render import ROLE_LABELS, fragment, page, safe, values


PACKET_STATES = {"sent": "已发出 · 待交回", "returned": "已交回 · 待复核",
                 "merged": "已合入", "withdrawn": "已撤回"}
EVENTS = {"issued": "发出", "returned": "交回", "merged": "合入",
          "merged_partial": "部分合入", "withdrawn": "撤回"}


def _selects(packet, entry):
    returned = (packet.get("returned") or {}).get("selections", {}).get(entry["id"])
    current = returned["final"] if returned else entry.get("choice")
    options = f'<option value="clear"{" selected" if current is None else ""}>保留未决</option>'
    options += "".join(f'<option value="{role}"{" selected" if role == current else ""}>{label}</option>'
                       for role, label in ROLE_LABELS.items())
    return (f'<label class="packet-choice"><span>{safe(entry["table"])} {values(entry["key"])}</span>'
            f'<select name="s_{safe(entry["id"])}">{options}</select></label>')


def rows(packet):
    return "".join(
        f'<tr><td>{safe(entry["table"])}</td><td>{values(entry["key"])}</td>'
        f'<td>{values(entry["base"])}</td><td>{values(entry["left"])}</td><td>{values(entry["right"])}</td>'
        f'<td>{ROLE_LABELS.get(entry["choice"], "待裁决")}</td></tr>' for entry in packet["items"])


def _text(entry):
    if entry["event"] == "issued":
        return f'第 {entry["plan_revision"]} 版发出 {entry["items"]} 行'
    if entry["event"] == "returned":
        return f'收件人交回 {entry["selections"]} 行选择'
    if entry["event"] in {"merged", "merged_partial"}:
        return (f'合入 {len(entry["applied"])} 行，待本人再决定 {len(entry["pending"])} 行 · '
                f'计划第 {entry["plan_revision_before"]} 版 → 第 {entry["plan_revision_after"]} 版')
    return f'撤回分发包（计划停留在第 {entry["plan_revision"]} 版）'


def log(packet):
    return "".join(f'<article class="attempt"><b>{EVENTS[entry["event"]]}</b>'
                   f'<small>{safe(_text(entry))}</small></article>'
                   for entry in packet["log"]) or '<p class="muted">暂无轨迹。</p>'


def packet_page(packet, plan):
    controls = ""
    if plan["state"] == "open" and packet["state"] in {"sent", "returned"}:
        if packet["state"] == "sent":
            controls = fragment("packet_return", id=packet["id"], digest=packet["digest"],
                                plan_revision=plan["revision"],
                                selects="".join(_selects(packet, entry) for entry in packet["items"]))
        controls += fragment("packet_controls", id=packet["id"], revision=plan["revision"])
    result = ""
    if packet["result"]:
        result = fragment("packet_result", summary=safe(describe(packet["result"])))
    elif packet["returned"]:
        result = fragment("packet_result", summary="收件人已交回选择，发件人尚未复核合入。")
    return page("packet", "裁决分发包", id=packet["id"], plan=packet["plan"], short=packet["id"][:12],
                recipient=safe(packet["recipient"]), revision=packet["plan_revision"],
                state=PACKET_STATES[packet["state"]], count=len(packet["items"]),
                rows=rows(packet), controls=controls, result=result, log=log(packet))
