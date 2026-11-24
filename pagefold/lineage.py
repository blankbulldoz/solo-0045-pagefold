from .codec import digest
from .errors import Problem
from .merge import ROLES

# 证据强度三档：封存输出（字节身份）与计划角色同为强证据，可并列成多条同等可信路线；
# 操作者声明与文件名推测为弱证据；来源缺失为无证据。
CONFIDENCE_RANK = {"strong": 3, "weak": 2, "none": 1}
ROLE_LABELS = {"base": "祖本", "left": "左支", "right": "右支",
               "claimed": "来源声明", "unknown": "来源不明"}
BASIS_LABELS = {
    "sealed_output": "封存输出（字节身份）", "plan_role": "计划角色",
    "declared": "操作者声明", "missing": "来源缺失", "filename": "文件名推测",
}
CONFIDENCE_LABELS = {"strong": "强", "weak": "弱", "none": "无"}
# 同一来源只允许一条边，依据与强度的合法搭配集中在此声明。
EDGE_SHAPE = {
    "sealed_output": "strong",
    "plan_role": "strong",
    "declared": "weak",
    "filename": "weak",
    "missing": "none",
}


def get_image(db, image_id, body=False):
    # 惰性导入以打破 images <-> lineage 的循环依赖。
    from .images import get_image as fetch_image
    return fetch_image(db, image_id, body=body)


def get_plan(db, plan_id):
    # 惰性导入以打破 plans -> images -> lineage 的循环依赖。
    from .plans import get_plan as fetch_plan
    return fetch_plan(db, plan_id)


def record_import(db, image_id, label):
    cur = db.execute("INSERT INTO imports(image_id,label) VALUES(?,?)", (image_id, label))
    return cur.lastrowid


def add_edge(db, image_id, basis, *, role="claimed", source_image_id=None, plan_id=None,
             import_id=None, note=""):
    if basis not in EDGE_SHAPE:
        raise Problem("lineage_basis", f"未知的来源依据：{basis}")
    confidence = EDGE_SHAPE[basis]
    db.execute(
        """INSERT INTO provenance_edges
           (image_id,role,source_image_id,plan_id,import_id,basis,confidence,note)
           VALUES(?,?,?,?,?,?,?,?)""",
        (image_id, role, source_image_id, plan_id, import_id, basis, confidence, note))


def record_declared(db, image_id, import_id, claims):
    """登记操作者随导入提供的来源声明。claims 为 64 位摘要列表，无法核验故仅为弱证据。"""
    if not isinstance(claims, list) or not 1 <= len(claims) <= 32:
        raise Problem("lineage_claims", "来源声明须是 1 至 32 个镜像摘要组成的列表。", 400)
    for token in claims:
        if not isinstance(token, str) or len(token) != 64:
            raise Problem("lineage_claims", "来源声明中的每项须是 64 位十六进制摘要。", 400)
        exists = db.execute("SELECT 1 FROM images WHERE id=?", (token,)).fetchone()
        # 声明指向未登记摘要时保留为悬空边，明确记录"声明了但核验不到"。
        add_edge(db, image_id, "declared", role="claimed",
                 source_image_id=token if exists else None, import_id=import_id,
                 note="已登记来源" if exists else "指向未登记镜像")


def record_plan_roles(db, plan_id, sources):
    row = get_plan(db, plan_id)
    for role in ROLES:
        add_edge(db, sources[role], "plan_role", role=role, plan_id=plan_id,
                 note=f"计划 {plan_id} 的{ROLE_LABELS[role]}角色")


def record_missing(db, image_id, import_id, note):
    add_edge(db, image_id, "missing", role="unknown", import_id=import_id, note=note)


def record_filename_hints(db, image_id, label, import_id):
    """
    同名陷阱：新镜像的标签与某个已登记镜像相同、字节摘要却不同。

    文件名只能提示“可能源自同一处”，无法核验，故记为弱证据，绝不与字节身份混同；
    即使同一镜像另有强边，弱边也保留，由调用方按 confidence 分层自行取舍。
    """
    if not label:
        return []
    hints = []
    for row in db.execute("SELECT id FROM images WHERE label=? AND id<>?", (label, image_id)):
        add_edge(db, image_id, "filename", role="claimed", source_image_id=row["id"],
                 import_id=import_id, note=f"标签“{label}”与已登记镜像同名，但字节摘要不同")
        hints.append(row["id"])
    return hints


def discover_sealed_outputs(db, image_id):
    """字节身份核验：该镜像是否与任一封存候选逐字节相同。相同字节可有不同推导来源，全部列出。"""
    image = get_image(db, image_id, body=True)
    matches = []
    for rel in db.execute("SELECT plan_id,candidate FROM releases").fetchall():
        if digest(rel["candidate"]) == image["id"]:
            plan = get_plan(db, rel["plan_id"])
            matches.append({"plan_id": rel["plan_id"], "revision": plan["revision"]})
            add_edge(db, image_id, "sealed_output", role="claimed", plan_id=rel["plan_id"],
                     note=f"与计划 {rel['plan_id']} 的封存候选字节相同")
    return matches


def edge_record(row):
    return {
        "id": row["id"], "image_id": row["image_id"], "role": row["role"],
        "role_label": ROLE_LABELS[row["role"]], "basis": row["basis"],
        "basis_label": BASIS_LABELS[row["basis"]], "confidence": row["confidence"],
        "confidence_rank": CONFIDENCE_RANK[row["confidence"]],
        "confidence_label": CONFIDENCE_LABELS[row["confidence"]],
        "source_image_id": row["source_image_id"], "plan_id": row["plan_id"],
        "import_id": row["import_id"], "note": row["note"], "created_at": row["created_at"],
    }


def image_lineage(db, image_id):
    get_image(db, image_id)
    imports = [dict(r) for r in db.execute(
        "SELECT id,label,occurred_at FROM imports WHERE image_id=? ORDER BY id", (image_id,))]
    edges = [edge_record(r) for r in db.execute(
        "SELECT * FROM provenance_edges WHERE image_id=? ORDER BY id", (image_id,))]
    edges.sort(key=lambda e: (-e["confidence_rank"], e["id"]))
    strong = [e for e in edges if e["confidence"] == "strong"]
    weak = [e for e in edges if e["confidence"] == "weak"]
    missing = [e for e in edges if e["confidence"] == "none"]
    return {
        "image_id": image_id, "imports": imports, "edges": edges,
        "strong_evidence": strong, "weak_evidence": weak, "missing_evidence": missing,
        "strongest_confidence": max((e["confidence"] for e in edges),
                                    key=lambda c: CONFIDENCE_RANK[c], default=None),
        "has_missing_edge": bool(missing),
    }


def plan_roles_of(db, image_id):
    """该镜像在哪些正式计划里扮演何种角色——同一文件可在不同计划复用。"""
    rows = db.execute(
        """SELECT p.id AS plan_id,p.state,p.revision,
                  CASE WHEN p.base=? THEN 'base' WHEN p.left_id=? THEN 'left' ELSE 'right' END AS role
           FROM plans p WHERE p.base=? OR p.left_id=? OR p.right_id=? ORDER BY p.rowid""",
        (image_id, image_id, image_id, image_id, image_id)).fetchall()
    return [{"plan_id": r["plan_id"], "state": r["state"], "revision": r["revision"],
             "role": r["role"], "role_label": ROLE_LABELS[r["role"]]} for r in rows]


def shared_inputs(db, candidate_sources):
    """
    启动新计划前的共同输入发现。

    比对候选三镜像的来源边，找出被多条推导路线共同指向的上游镜像。
    每条共同输入同时报告其证据强度：强证据来自字节可核验的计划角色/封存输出，
    弱证据来自操作者声明，缺失边单独提示。返回已按强度排序、可直接展示的结构。
    """
    roles = [r for r in ROLES if r in candidate_sources]
    if not roles:
        raise Problem("lineage_sources", "至少需要一个候选镜像角色。", 400)
    for role in roles:
        get_image(db, candidate_sources[role])
    aggregate = {}
    per_role = {}
    for role in roles:
        image_id = candidate_sources[role]
        lineage_info = image_lineage(db, image_id)
        per_role[role] = {
            "image_id": image_id,
            "strongest_confidence": lineage_info["strongest_confidence"],
            "has_missing_edge": lineage_info["has_missing_edge"],
            "edge_count": len(lineage_info["edges"]),
        }
        for edge in lineage_info["edges"]:
            anchor = edge["source_image_id"] or edge["plan_id"]
            if anchor is None:
                continue
            entry = aggregate.setdefault(anchor, {
                "anchor": anchor, "kind": "plan" if edge["plan_id"] else "image",
                "source_image_id": edge["source_image_id"], "plan_id": edge["plan_id"],
                "routes": [], "max_confidence_rank": 0})
            entry["routes"].append({
                "via_role": role, "via_image": image_id, "basis": edge["basis"],
                "basis_label": BASIS_LABELS[edge["basis"]], "confidence": edge["confidence"],
                "confidence_label": CONFIDENCE_LABELS[edge["confidence"]],
                "note": edge["note"]})
            entry["max_confidence_rank"] = max(entry["max_confidence_rank"],
                                               CONFIDENCE_RANK[edge["confidence"]])
    common = [entry for entry in aggregate.values()
              if len({route["via_role"] for route in entry["routes"]}) >= 2]
    for entry in common:
        entry["max_confidence"] = next(
            c for c, rank in CONFIDENCE_RANK.items() if rank == entry["max_confidence_rank"])
        entry["max_confidence_label"] = CONFIDENCE_LABELS[entry["max_confidence"]]
        entry["route_count"] = len(entry["routes"])
        # 入边证据可能只是口头声明，但锚点镜像本身或许可由字节核验出强身份，两者分别报告。
        anchor_evidence = None
        if entry["source_image_id"]:
            anchor_lineage = image_lineage(db, entry["source_image_id"])
            anchor_evidence = anchor_lineage["strongest_confidence"]
        entry["anchor_self_confidence"] = anchor_evidence
        entry["anchor_self_confidence_label"] = (
            CONFIDENCE_LABELS[anchor_evidence] if anchor_evidence else "无")
    common.sort(key=lambda e: (-e["max_confidence_rank"], e["anchor"]))
    ambiguous = [r for r in per_role.values() if r["has_missing_edge"]]
    return {
        "candidate_roles": per_role,
        "common_inputs": common,
        "common_count": len(common),
        "ambiguous_roles": ambiguous,
        "ambiguous_count": len(ambiguous),
    }


def derivation_routes(db, image_id):
    """
    列出指向某镜像的全部同等可信推导路线。

    封存输出与字节身份同为强证据；当存在多条强边时，意味着同一字节可由多条
    独立、同等可信的路线解释，调用方应一并可查，而不是任择其一。
    """
    lineage = image_lineage(db, image_id)
    strong = lineage["strong_evidence"]
    return {
        "image_id": image_id,
        "routes": strong,
        "route_count": len(strong),
        "plural_strong_routes": len(strong) >= 2,
        "weak_evidence": lineage["weak_evidence"],
        "missing_evidence": lineage["missing_evidence"],
    }


def preflight(store, sources):
    with store.connect() as db:
        return shared_inputs(db, sources)


def build_lineage_view(db, image_id):
    result = image_lineage(db, image_id)
    result["plan_roles"] = plan_roles_of(db, image_id)
    result["derivation"] = derivation_routes(db, image_id)
    return result
