import json
import uuid

from .codec import canonical, fingerprint
from .decisions import record as log_decision
from .errors import Problem
from .images import get_image
from .merge import ROLES
from .plans import expect_open, expect_revision, get_plan, plan_content
from .render import ROLE_LABELS


PACKET_FORMAT = "pagefold-packet-v1"
RETURN_FORMAT = "pagefold-packet-return-v1"
CHOICES = (*ROLES, "clear")
MAX_STEPS = 8


def conflict_items(items):
    return [item for item in items if item["automatic"] is None]


def row_digest(row):
    return fingerprint(row)


def _record(item, decisions):
    return {"id": item["id"], "table": item["table"], "key": item["key"],
            "base": item["base"], "left": item["left"], "right": item["right"],
            "choice": decisions.get(item["id"]), "row_digest": row_digest(
                [item["base"], item["left"], item["right"]])}


def _tables(catalog, records):
    names = {record["table"] for record in records}
    return {table["name"]: {"columns": table["columns"], "key": table["key"]}
            for table in catalog["tables"] if table["name"] in names}


def _order(item):
    return (item["table"], canonical(item["key"]))


def _body(packet_id, plan, recipient, revision, records, catalog):
    return {"format": PACKET_FORMAT, "id": packet_id, "plan": plan["id"],
            "recipient": recipient, "plan_revision": revision,
            "sources": {"base": plan["base"], "left": plan["left_id"], "right": plan["right_id"]},
            "tables": _tables(catalog, records), "items": records}


def _row(db, packet_id):
    row = db.execute("SELECT * FROM packets WHERE id=?", (packet_id,)).fetchone()
    if row is None:
        raise Problem("unknown_packet", "找不到指定的分发包。", 404)
    return row


def _render(row):
    body = json.loads(row["items"])
    return dict(body, digest=fingerprint(body), state=row["state"],
                returned=json.loads(row["returned"]) if row["returned"] else None,
                result=json.loads(row["result"]) if row["result"] else None,
                applied_revision=row["applied_revision"], log=json.loads(row["log"]))


def export(row):
    """收件人手持的独立交付物：只含分发行的三方值与裁决必要值。"""
    body = json.loads(row["items"])
    return {key: body[key] for key in ("format", "id", "plan", "recipient", "plan_revision",
                                       "sources", "tables", "items")} | {"digest": fingerprint(body)}


def get_packet(db, packet_id):
    row = _row(db, packet_id)
    return dict(_render(row), export=export(row))


def read_packet(store, packet_id):
    with store.connect() as db:
        return get_packet(db, packet_id)


def _summary(row):
    packet, result = json.loads(row["items"]), json.loads(row["result"]) if row["result"] else None
    return {"id": row["id"], "plan": row["plan_id"], "recipient": row["recipient"], "state": row["state"],
            "plan_revision": row["revision"], "applied_revision": row["applied_revision"],
            "items": len(packet["items"]), "digest": fingerprint(packet),
            "applied": len(result["applied"]) if result else 0,
            "pending": len(result["pending"]) if result else 0}


def list_packets(db, plan_id):
    get_plan(db, plan_id)
    return [_summary(row) for row in db.execute(
        "SELECT * FROM packets WHERE plan_id=? ORDER BY rowid", (plan_id,))]


def issue(store, plan_id, revision, recipient, item_ids=None):
    recipient = (recipient or "").strip()
    if not 1 <= len(recipient) <= 100:
        raise Problem("recipient", "请填写收件人，长度不超过 100 字。", 400)
    if item_ids is not None and not isinstance(item_ids, (list, tuple)):
        raise Problem("items", "请以列表指定要分发的行。", 400)
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        items, decisions = plan_content(plan)
        conflicts = conflict_items(items)
        if item_ids is None:
            chosen = conflicts
        else:
            wanted = set(item_ids)
            if not all(isinstance(item_id, str) for item_id in wanted):
                raise Problem("items", "分发行的标识须为摘要。", 400)
            if wanted - {item["id"] for item in conflicts}:
                raise Problem("not_conflict", "分发包只能包含当前仍待裁决的冲突行。", 409)
            chosen = [item for item in conflicts if item["id"] in wanted]
        if not chosen:
            raise Problem("no_items", "没有可分发的冲突行。", 409)
        records = [_record(item, decisions) for item in sorted(chosen, key=_order)]
        catalog = json.loads(get_image(db, plan["base"])["catalog"])
        packet_id = uuid.uuid4().hex
        body = _body(packet_id, plan, recipient, plan["revision"], records, catalog)
        log = [{"event": "issued", "plan_revision": plan["revision"], "items": len(records)}]
        db.execute("INSERT INTO packets(id,plan_id,recipient,revision,state,items,returned,result,"
                   "applied_revision,log) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (packet_id, plan_id, recipient, plan["revision"], "sent", canonical(body),
                    None, None, None, canonical(log)))
        return _render(_row(db, packet_id))


def _steps(raw, item_id):
    sequence = [raw] if isinstance(raw, str) else raw
    if (not isinstance(sequence, list) or not 1 <= len(sequence) <= MAX_STEPS
            or any(step not in CHOICES for step in sequence)):
        raise Problem("selection", f"行 {item_id} 的选择须为祖本、左支、右支或清除。", 400)
    return {"sequence": sequence, "final": None if sequence[-1] == "clear" else sequence[-1],
            "cleared": "clear" in sequence}


def accept_return(store, packet_id, digest, selections, note=None):
    if not isinstance(selections, dict):
        raise Problem("selection", "请提交行选择对象。", 400)
    if note is not None and (not isinstance(note, str) or len(note) > 500):
        raise Problem("note", "备注须为不超过 500 字的文本。", 400)
    with store.connect(write=True) as db:
        row = _row(db, packet_id)
        if row["state"] in {"merged", "withdrawn"}:
            raise Problem("packet_closed", "该分发包已经结清，不能再交回选择。", 409)
        packet = json.loads(row["items"])
        if digest != fingerprint(packet):
            raise Problem("packet_digest", "交回内容与分发包摘要不匹配。", 409)
        ids = {item["id"] for item in packet["items"]}
        unknown = set(selections) - ids
        if unknown:
            raise Problem("out_of_packet", "选择包含未分发到本包的行。", 409)
        normalized = {item_id: _steps(raw, item_id) for item_id, raw in sorted(selections.items())}
        returned = {"format": RETURN_FORMAT, "packet": packet_id, "digest": digest,
                    "plan_revision": packet["plan_revision"], "selections": normalized}
        if note:
            returned["note"] = note
        log = json.loads(row["log"])
        log.append({"event": "returned", "selections": len(normalized), "note": bool(note)})
        db.execute("UPDATE packets SET state='returned',returned=?,log=? WHERE id=?",
                   (canonical(returned), canonical(log), packet_id))
        return _render(_row(db, packet_id))


def _note(answer, record, holder):
    if answer["cleared"] and answer["sequence"][-1] != "clear":
        return "cleared_then_restored"
    if holder != record["choice"]:
        return "converged_elsewhere"
    return "already_same"


def _classify(record, item, decisions, answer, own, fresh):
    holder = decisions.get(record["id"])
    final = answer["final"]
    if row_digest([item["base"], item["left"], item["right"]]) != record["row_digest"]:
        return "pending", {"reason": "stale_item", "detail": "该行的三方值与分发时不同，需重新确认。"}
    if holder == final:
        return "unchanged", {"choice": holder, "note": _note(answer, record, holder)}
    if record["id"] in fresh:
        # 本包上次合入已把这一行列为待本人再决定，本次交回即本人的再次确认。
        return "applied", {"previous": holder, "choice": final, "sequence": answer["sequence"],
                           "note": "confirmed_after_conflict"}
    # 仅当本包上次写入的判断仍是当前值，才视为本人在更新自己的选择；
    # 否则说明分发后有别人为这一行做过决定，必须本人再确认。
    if (record["id"] not in own or holder != own[record["id"]]) and holder != record["choice"]:
        return "pending", {"reason": "concurrent_choice", "returned": final, "current": holder,
                           "detail": f"分发后该行已被他人裁为「{ROLE_LABELS.get(holder, '未决')}」，"
                                     "本人的选择需确认后才能合入。"}
    return "applied", {"previous": holder, "choice": final, "sequence": answer["sequence"]}


def merge(store, packet_id, revision):
    with store.connect(write=True) as db:
        row = _row(db, packet_id)
        if row["state"] == "merged":
            return json.loads(row["result"])
        if row["state"] == "withdrawn":
            raise Problem("packet_closed", "该分发包已撤回。", 409)
        if row["state"] != "returned":
            raise Problem("packet_unreturned", "该分发包尚未交回选择。", 409)
        plan = get_plan(db, row["plan_id"])
        expect_revision(plan, revision)
        expect_open(plan)
        result, trace = _settle(db, row, plan)
        db.execute("UPDATE packets SET state=?,result=?,applied_revision=?,log=? WHERE id=?",
                   (result["state"], canonical(result), result["plan_revision_after"],
                    canonical(trace), packet_id))
        return result


def _settle(db, row, plan):
    items, decisions = plan_content(plan)
    packet = json.loads(row["items"])
    returned = json.loads(row["returned"])
    selections = returned["selections"]
    previous = json.loads(row["result"]) if row["result"] else None
    own = {entry["item"]: entry["choice"] for entry in previous["applied"]} if previous else {}
    fresh = {entry["item"] for entry in previous["pending"]} if previous else set()
    current = {item["id"]: item for item in items}
    before = plan["revision"]
    applied, unchanged, pending, omitted = [], [], [], []
    for record in packet["items"]:
        answer = selections.get(record["id"])
        item = current.get(record["id"])
        entry = {"item": record["id"], "table": record["table"], "key": record["key"],
                 "again": record["id"] in fresh}
        if item is None:
            pending.append(dict(entry, reason="stale_item", detail="该行已不在当前计划中，需重新确认。"))
            continue
        if answer is None:
            omitted.append(entry)
            continue
        verdict, detail = _classify(record, item, decisions, answer, own, fresh)
        entry.update(detail)
        {"applied": applied, "unchanged": unchanged, "pending": pending}[verdict].append(entry)
    for entry in applied:
        if entry["choice"] is None:
            decisions.pop(entry["item"], None)
        else:
            decisions[entry["item"]] = entry["choice"]
    after = before + 1 if applied else before
    if applied:
        db.execute("UPDATE plans SET decisions=?,revision=? WHERE id=?",
                   (canonical(decisions), after, plan["id"]))
        log_decision(db, plan["id"], after, [(entry["item"], entry["previous"], entry["choice"])
                                             for entry in applied], "packet", row["recipient"])
    result = {"packet": row["id"], "plan": plan["id"], "recipient": row["recipient"],
              "state": "merged" if not pending else "returned",
              "plan_revision_before": before, "plan_revision_after": after,
              "applied": applied, "unchanged": unchanged, "pending": pending, "omitted": omitted,
              "packet_digest": fingerprint(packet), "return_digest": fingerprint(returned),
              "decision_digest": fingerprint(decisions)}
    trace = json.loads(row["log"])
    trace.append({"event": "merged" if not pending else "merged_partial",
                  "plan_revision_before": before, "plan_revision_after": after,
                  "applied": [{"item": entry["item"], "choice": entry["choice"]} for entry in applied],
                  "unchanged": [entry["item"] for entry in unchanged],
                  "pending": [entry["item"] for entry in pending]})
    return result, trace


def withdraw(store, packet_id, revision):
    with store.connect(write=True) as db:
        row = _row(db, packet_id)
        if row["state"] in {"merged", "withdrawn"}:
            raise Problem("packet_closed", "该分发包已经结清。", 409)
        plan = get_plan(db, row["plan_id"])
        expect_revision(plan, revision)
        expect_open(plan)
        log = json.loads(row["log"])
        log.append({"event": "withdrawn", "plan_revision": plan["revision"]})
        db.execute("UPDATE packets SET state='withdrawn',log=? WHERE id=?",
                   (canonical(log), packet_id))
        return _render(_row(db, packet_id))


def describe(result):
    lines = []
    if result["applied"]:
        lines.append("安全并入：" + "、".join(
            f'{entry["table"]} {canonical(entry["key"])} '
            f'（{ROLE_LABELS.get(entry["previous"], "未决")} → {ROLE_LABELS.get(entry["choice"], "未决")}）'
            for entry in result["applied"]))
    if result["unchanged"]:
        lines.append("无需改动：" + "、".join(
            f'{entry["table"]} {canonical(entry["key"])}' for entry in result["unchanged"]))
    if result["pending"]:
        lines.append("需本人再决定：" + "；".join(
            f'{entry["table"]} {canonical(entry["key"])} {entry["detail"]}' for entry in result["pending"]))
    if result["omitted"]:
        lines.append("未交回：" + "、".join(
            f'{entry["table"]} {canonical(entry["key"])}' for entry in result["omitted"]))
    return " ".join(lines) or "没有交回任何选择。"
