import json

from .codec import fingerprint
from .errors import Problem
from .images import get_image
from .merge import ROLES, calculate
from .rows import read_image
from .sqlite_image import mounted

# 某一主键行在某个祖本视角下的处置状态。
STATUS_ABSENT = "absent"        # 三方同值（含同缺席），按规则不进入计划
STATUS_AUTOMATIC = "automatic"  # 自动采纳，automatic 指向具体来源角色
STATUS_CONFLICT = "conflict"    # 双边不同且无单边跟随，待裁决


def _load_snapshots(db, sources):
    images = [get_image(db, sources[role], body=True) for role in ROLES]
    catalogs = [json.loads(image["catalog"]) for image in images]
    if len({catalog["signature"] for catalog in catalogs}) != 1:
        raise Problem("schema_mismatch", "参与试算的镜像模式记录不一致。", 409)
    snapshots = []
    for image, catalog in zip(images, catalogs):
        with mounted(image["body"]) as snapshot:
            snapshots.append(read_image(snapshot, catalog))
    return snapshots


def _entry_index(items):
    return {entry["id"]: entry for entry in items}


def _row_status(entry):
    if entry is None:
        return STATUS_ABSENT
    return STATUS_CONFLICT if entry["automatic"] is None else STATUS_AUTOMATIC


def _preview(label, base_id, snapshots):
    items = calculate(*snapshots)
    rows = [{
        "id": entry["id"], "table": entry["table"], "key": entry["key"],
        "status": _row_status(entry), "automatic": entry["automatic"],
        "selected_role": entry["automatic"],
        "selected_row": entry[entry["automatic"]] if entry["automatic"] else None,
        "base": entry["base"], "left": entry["left"], "right": entry["right"],
    } for entry in items]
    counts = {
        "items": len(items),
        "automatic": sum(row["status"] == STATUS_AUTOMATIC for row in rows),
        "conflict": sum(row["status"] == STATUS_CONFLICT for row in rows),
        "absent": 0,
    }
    return {"label": label, "base": base_id, "plan_digest": fingerprint(items),
            "counts": counts, "items": items, "index": _entry_index(items), "rows": rows}


def _all_keys(previews):
    keys = {}
    for preview in previews:
        for row in preview["rows"]:
            keys.setdefault(row["id"], {"table": row["table"], "key": row["key"]})
    return keys


def _aligned_rows(previews):
    aligned = []
    for entry_id, spot in sorted(_all_keys(previews).items()):
        sides = []
        for preview in previews:
            entry = preview["index"].get(entry_id)
            sides.append({
                "label": preview["label"],
                "status": _row_status(entry),
                "automatic": entry["automatic"] if entry else None,
                "base": entry["base"] if entry else None,
                "left": entry["left"] if entry else None,
                "right": entry["right"] if entry else None,
            })
        statuses = {view["status"] for view in sides}
        automatic_roles = {view["automatic"] for view in sides if view["automatic"] is not None}
        flips = [
            {"from": sides[i]["label"], "to": sides[j]["label"],
             "role_from": sides[i]["automatic"], "role_to": sides[j]["automatic"]}
            for i in range(len(sides))
            for j in range(len(sides))
            if i < j
            and sides[i]["status"] == STATUS_AUTOMATIC
            and sides[j]["status"] == STATUS_AUTOMATIC
            and sides[i]["automatic"] != sides[j]["automatic"]
        ]
        values_differ = any(
            any(sides[0][role] != view[role] for role in ROLES) for view in sides[1:])
        # 实质不同：状态类别变化、自动采纳角色翻转，或同一主键的三方取值有别。
        materially_different = (
            len(statuses) > 1 or len(automatic_roles) > 1 or values_differ)
        aligned.append({
            "id": entry_id, "table": spot["table"], "key": spot["key"],
            "statuses": {view["label"]: view["status"] for view in sides},
            "automatic": {view["label"]: view["automatic"] for view in sides},
            "materially_different": materially_different,
            "automatic_flips": flips,
            "sides": sides,
        })
    return aligned


def _summarize(aligned, previews):
    labels = [preview["label"] for preview in previews]
    first, second = labels[0], labels[1]
    flipped = []
    vanished, appeared, resolved, introduced, material = [], [], [], [], []
    for row in aligned:
        status_first = row["statuses"][first]
        status_second = row["statuses"][second]
        if row["automatic_flips"]:
            flipped.append(row["id"])
        if status_first != STATUS_ABSENT and status_second == STATUS_ABSENT:
            vanished.append(row["id"])
        if status_first == STATUS_ABSENT and status_second != STATUS_ABSENT:
            appeared.append(row["id"])
        if status_first == STATUS_CONFLICT and status_second != STATUS_CONFLICT:
            resolved.append({"id": row["id"], "became": status_second})
        if status_first != STATUS_CONFLICT and status_second == STATUS_CONFLICT:
            introduced.append({"id": row["id"], "from": status_first})
        if row["materially_different"]:
            material.append(row["id"])
    totals = {preview["label"]: preview["counts"]["items"] for preview in previews}
    conflicts = {preview["label"]: preview["counts"]["conflict"] for preview in previews}
    return {
        "labels": labels,
        "totals": totals,
        "conflicts": conflicts,
        "same_total": len(set(totals.values())) == 1,
        "automatic_flip_items": flipped,
        "vanished_items": vanished,
        "appeared_items": appeared,
        "conflict_resolved_items": resolved,
        "conflict_introduced_items": introduced,
        "materially_different_items": material,
        "materially_different_count": len(material),
    }


def build_trial(store, left_id, right_id, bases):
    """对同一对左右分支、两个候选祖本做不保存的并列试算。

    bases 为有序的 [(标签, 镜像摘要), (标签, 镜像摘要)]，顺序即上下两份预览的次序；
    不向状态库写入任何计划、裁决或重演记录。
    """
    if len(bases) != 2 or len({label for label, _ in bases}) != 2:
        raise Problem("trial_bases", "请提供两个标签不同的候选祖本。", 400)
    with store.connect() as db:
        previews = []
        for label, base_id in bases:
            snapshots = _load_snapshots(
                db, {"base": base_id, "left": left_id, "right": right_id})
            previews.append(_preview(label, base_id, snapshots))
        aligned = _aligned_rows(previews)
        comparison = _summarize(aligned, previews)
    return {
        "format": "pagefold-lineage-trial-v1",
        "saved": False,
        "roles": list(ROLES),
        "left": left_id,
        "right": right_id,
        "bases": [{"label": label, "id": base_id} for label, base_id in bases],
        "previews": [{key: value for key, value in preview.items() if key != "index"}
                     for preview in previews],
        "aligned": aligned,
        "comparison": comparison,
    }


def reproduce_plan_payload(trial, label):
    """从试算结果复刻某一侧正式计划所需的来源参数。"""
    base = next((item for item in trial["bases"] if item["label"] == label), None)
    if base is None:
        raise Problem("unknown_preview", "试算中没有该祖本视角。", 404)
    return {"base": base["id"], "left": trial["left"], "right": trial["right"]}


def preview_items(trial, label):
    preview = next((item for item in trial["previews"] if item["label"] == label), None)
    if preview is None:
        raise Problem("unknown_preview", "试算中没有该祖本视角。", 404)
    return preview
