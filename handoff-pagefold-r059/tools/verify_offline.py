#!/usr/bin/env python3
"""Offline verifier/query helper for the Pagefold r059 handoff package.

Only the Python standard library is required.  It never starts Pagefold's HTTP
service.  In addition to hash/certificate checks, it opens the sealed SQLite
file read-only and checks the business rows that prove the merge was not a
whole-database overwrite by the right branch.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "handoff-manifest.json"
CERT_PATH = ROOT / "certificate" / "pagefold-certificate.json"
ROLES_PATH = ROOT / "sources" / "SOURCE-ROLES.json"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical(value: Any) -> str:
    # Must match pagefold.codec.canonical exactly.
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def fingerprint(value: Any) -> str:
    return sha256_bytes(canonical(value).encode("ascii"))


def sqlite_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def schema_signature(path: Path) -> str:
    """Compute pagefold.catalog's signature without importing Pagefold."""
    db = sqlite3.connect(sqlite_uri(path), uri=True)
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        rows = db.execute(
            "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
            "WHERE substr(name,1,7) <> 'sqlite_' ORDER BY type,name"
        ).fetchall()
    finally:
        db.close()
    return fingerprint(rows)


def rel(path: Path, base: Path = ROOT) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


def package_relative_files() -> set[str]:
    files: set[str] = set()
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        name = path.relative_to(ROOT).as_posix()
        if name == "handoff-manifest.json":
            continue
        files.add(name)
    return files


def verify_manifest(checks: list[tuple[str, bool, str]]) -> dict[str, Any] | None:
    if not MANIFEST_PATH.is_file():
        checks.append(("交接清单存在", False, "缺少 handoff-manifest.json"))
        return None
    try:
        manifest = load_json(MANIFEST_PATH)
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(("交接清单可解析", False, str(exc)))
        return None
    checks.append(("交接清单可解析", True, MANIFEST_PATH.name))

    entries = manifest.get("files", [])
    listed = {entry["path"]: entry for entry in entries if isinstance(entry, dict) and "path" in entry}
    actual = package_relative_files()
    missing = sorted(set(listed) - actual)
    extra = sorted(actual - set(listed))
    checks.append(("无清单外文件（可发现混放文件）", not missing and not extra,
                   f"missing={missing or '无'}; extra={extra or '无'}"))

    bad: list[str] = []
    for name, entry in listed.items():
        path = ROOT / name
        if not path.is_file():
            bad.append(f"{name}: missing")
            continue
        digest = sha256_file(path)
        size = path.stat().st_size
        if digest != entry.get("sha256") or size != entry.get("size"):
            bad.append(name)
    checks.append(("清单内全部 SHA-256/大小一致", not bad, "不一致=" + (", ".join(bad) if bad else "无")))

    sums_path = ROOT / "SHA256SUMS"
    sums_bad: list[str] = []
    sums_checked = 0
    if sums_path.is_file():
        for line_no, line in enumerate(sums_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            parts = line.split(" ", 1)
            if len(parts) != 2 or len(parts[0]) != 64:
                sums_bad.append(f"line {line_no}: format")
                continue
            expected, target = parts[0], parts[1]
            if target.startswith(("*", " ")):
                target = target[1:]
            target_path = Path(target)
            if target_path.is_absolute() or ".." in target_path.parts:
                sums_bad.append(f"{target}: unsafe path")
                continue
            path = ROOT / target
            if not path.is_file() or sha256_file(path) != expected:
                sums_bad.append(target)
            else:
                sums_checked += 1
        checks.append(("SHA256SUMS 标准校验和", not sums_bad,
                       f"核对 {sums_checked} 项; 异常={sums_bad or '无'}"))
    else:
        checks.append(("SHA256SUMS 标准校验和", False, "缺少 SHA256SUMS"))
    return manifest


def load_trust_inputs(checks: list[tuple[str, bool, str]]):
    try:
        cert = load_json(CERT_PATH)
        roles_doc = load_json(ROLES_PATH)
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(("凭证和来源角色可读", False, str(exc)))
        return None, None
    roles = roles_doc.get("roles", {})
    required = {"base", "left", "right"}
    checks.append(("凭证和来源角色可读", required <= set(roles), f"roles={sorted(roles)}"))
    return cert, roles


def verify_sources_and_certificate(
    checks: list[tuple[str, bool, str]], cert: dict[str, Any], roles: dict[str, Any]
) -> Path | None:
    source_bad: list[str] = []
    for role in ("base", "left", "right"):
        meta = roles.get(role, {})
        path = ROOT / meta.get("file", "")
        expected = cert.get("sources", {}).get(role)
        if not path.is_file():
            source_bad.append(f"{role}: file missing")
            continue
        digest = sha256_file(path)
        if digest != expected or digest != meta.get("sha256") or expected != meta.get("image_id"):
            source_bad.append(role)
    checks.append(("三来源文件哈希绑定凭证角色", not source_bad,
                   "异常=" + (", ".join(source_bad) if source_bad else "无")))

    result_rel = "result/merged-r059-right-choice.sqlite"
    result_path = ROOT / result_rel
    if not result_path.is_file():
        checks.append(("封存成果存在且摘要匹配凭证", False, result_rel))
        return None
    digest = sha256_file(result_path)
    candidate_ok = digest == cert.get("candidate_digest") and result_path.stat().st_size == cert.get("candidate_size")
    checks.append(("封存成果存在且摘要匹配凭证", candidate_ok, digest))

    schema_bad: list[str] = []
    expected_schema = cert.get("schema")
    for role in ("base", "left", "right"):
        path = ROOT / roles[role]["file"]
        if schema_signature(path) != expected_schema:
            schema_bad.append(role)
    if schema_signature(result_path) != expected_schema:
        schema_bad.append("result")
    checks.append(("三来源与封存结果模式摘要一致", not schema_bad,
                   f"schema={expected_schema}; 异常={', '.join(schema_bad) if schema_bad else '无'}"))

    initial_plan = load_json(ROOT / "audit" / "initial-plan-response.json")
    raw_items = [{k: v for k, v in item.items() if k != "choice"} for item in initial_plan["items"]]
    plan_ok = fingerprint(raw_items) == cert.get("plan_digest")
    checks.append(("计划摘要可由初始计划重算", plan_ok, cert.get("plan_digest", "")))

    decision_response = load_json(ROOT / "audit" / "decision-response.json")
    decisions = {decision_response["item"]: decision_response["choice"]}
    decision_ok = fingerprint(decisions) == cert.get("decision_digest")
    checks.append(("裁决摘要可由裁决记录重算", decision_ok, cert.get("decision_digest", "")))

    final_plan = load_json(ROOT / "audit" / "final-plan.json")
    state_ok = final_plan.get("state") == "archived" and final_plan.get("revision") == cert.get("revision") == 1
    checks.append(("计划版本/封存后归档状态", state_ok,
                   f"revision={final_plan.get('revision')}, state={final_plan.get('state')}"))
    return result_path if candidate_ok else None


def verify_state_backup(checks: list[tuple[str, bool, str]], cert: dict[str, Any]) -> None:
    path = ROOT / "audit" / "pagefold-state-backup.sqlite"
    if not path.is_file():
        checks.append(("服务端状态备份中的封存记录", False, "缺少 audit/pagefold-state-backup.sqlite"))
        return
    db = sqlite3.connect(sqlite_uri(path), uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        plan = db.execute(
            "SELECT revision,state FROM plans WHERE id=?", (cert["plan"],)
        ).fetchone()
        release = db.execute(
            "SELECT certificate,candidate FROM releases WHERE plan_id=?", (cert["plan"],)
        ).fetchone()
        if plan is None or release is None:
            checks.append(("服务端状态备份中的封存记录", False, "plan/release not found"))
            return
        stored_cert = json.loads(release["certificate"])
        candidate_digest = sha256_bytes(bytes(release["candidate"]))
        ok = (
            plan["revision"] == cert["revision"]
            and plan["state"] == "archived"
            and stored_cert == cert
            and candidate_digest == cert["candidate_digest"]
        )
        checks.append((
            "服务端状态备份中的封存记录",
            ok,
            f"state={plan['state']}, revision={plan['revision']}, candidate={candidate_digest}",
        ))
    finally:
        db.close()


def verify_database_business_evidence(checks: list[tuple[str, bool, str]], result_path: Path) -> dict[str, Any]:
    db = sqlite3.connect(sqlite_uri(result_path), uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        integrity = db.execute("PRAGMA integrity_check(1)").fetchone()[0]
        fk = db.execute("PRAGMA foreign_key_check").fetchall()
        checks.append(("SQLite integrity_check", integrity == "ok", str(integrity)))
        checks.append(("SQLite foreign_key_check", not fk, f"violations={len(fk)}"))

        node_count = db.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        keys = [tuple(r) for r in db.execute("SELECT bench,slot FROM node ORDER BY bench,slot")]
        expected_keys = [("north", 1), ("south", 1), ("south", 2)]
        checks.append(("左支新增复合键 ('south',2) 仍在封存库",
                       node_count == 3 and keys == expected_keys, f"keys={keys}"))

        north = db.execute(
            "SELECT bench,slot,note,payload,weight FROM node WHERE bench=? AND slot=?",
            ("north", 1),
        ).fetchone()
        blob = bytes(north["payload"])
        north_ok = (
            north["note"] == "祖本节点"
            and blob == bytes.fromhex("ff00")
            and north["weight"] == 2.25
        )
        blob_detail = f"key=('north',1), hex={blob.hex()}, base64={base64.b64encode(blob).decode()}"
        checks.append(("冲突复合键 ('north',1) 采用右支整行/BLOB", north_ok, blob_detail))

        added = db.execute(
            "SELECT bench,slot,note,payload,weight FROM node WHERE bench=? AND slot=?",
            ("south", 2),
        ).fetchone()
        added_ok = (
            added is not None and added["note"] == "左支新增" and added["payload"] is None
            and added["weight"] == 4.0
        )
        checks.append(("左支新增业务值完整保留", added_ok,
                       "('south',2) note=左支新增,payload=NULL,weight=4.0"))

        label = db.execute("SELECT label FROM bench WHERE code='south'").fetchone()[0]
        checks.append(("右支自动更名保留", label == "右支更名", f"bench.south.label={label}"))

        binding = db.execute(
            "SELECT id,bench,slot FROM binding WHERE id=1 AND bench='north' AND slot=1"
        ).fetchone()
        checks.append(("外键绑定行仍指向冲突复合键", binding is not None, "binding id=1 -> ('north',1)"))

        return {"north": dict(north), "blob_hex": blob.hex(), "keys": [list(k) for k in keys]}
    finally:
        db.close()


def scan_source_dir(directory: Path, cert: dict[str, Any]) -> dict[str, Any]:
    trusted = {digest: role for role, digest in cert.get("sources", {}).items()}
    records: list[dict[str, Any]] = []
    role_counts: dict[str, int] = {role: 0 for role in trusted.values()}

    for path in sorted(directory.rglob("*")) if directory.exists() else []:
        if not path.is_file():
            continue
        name = rel(path, directory)
        if not is_sqlite_file(path):
            records.append({"file": name, "classification": "non_sqlite_ignored"})
            continue
        digest = sha256_file(path)
        if digest in trusted:
            role = trusted[digest]
            role_counts[role] += 1
            records.append({
                "file": name,
                "classification": f"trusted_{role}",
                "sha256": digest,
            })
            continue
        try:
            signature = schema_signature(path)
        except sqlite3.Error as exc:
            records.append({
                "file": name,
                "classification": "unreadable_sqlite",
                "sha256": digest,
                "detail": str(exc),
            })
            continue
        same_schema = signature == cert.get("schema")
        records.append({
            "file": name,
            "classification": "foreign_same_schema" if same_schema else "foreign_other_schema",
            "sha256": digest,
            "schema_signature": signature,
            "schema_matches_certificate": same_schema,
        })

    foreign_same = [r for r in records if r["classification"] == "foreign_same_schema"]
    foreign_other = [r for r in records if r["classification"] == "foreign_other_schema"]
    unreadable = [r for r in records if r["classification"] == "unreadable_sqlite"]
    missing = sorted(role for role, count in role_counts.items() if count != 1)
    duplicate = sorted(role for role, count in role_counts.items() if count > 1)
    ok = not foreign_same and not foreign_other and not unreadable and not missing and not duplicate
    return {
        "directory": str(directory),
        "ok": ok,
        "mixed_in_same_schema": bool(foreign_same),
        "unreadable_sqlite": bool(unreadable),
        "missing_or_duplicate_roles": missing + duplicate,
        "missing_roles": [r for r in missing if role_counts[r] == 0],
        "duplicate_roles": duplicate,
        "records": records,
    }


def verify_package_source_scan(checks, cert):
    report = scan_source_dir(ROOT / "sources", cert)
    bad = [
        r["file"] for r in report["records"]
        if r["classification"].startswith("foreign") or r["classification"] == "unreadable_sqlite"
    ]
    checks.append(("sources/ 未混进同模式/异模式/不可读 SQLite", report["ok"],
                   "异常=" + (", ".join(bad) if bad else "无")))
    return report


def mixup_self_test() -> bool:
    cert = load_json(CERT_PATH)
    roles = load_json(ROLES_PATH)["roles"]
    impostor = ROOT / "negative-tests" / "stress-left-same-schema.sqlite"
    if not impostor.is_file():
        print("缺少 negative-tests/stress-left-same-schema.sqlite", file=sys.stderr)
        return False

    with tempfile.TemporaryDirectory(prefix="pagefold-mixup-test-") as temp:
        temp_path = Path(temp)
        mixed = temp_path / "incoming-mixed"
        replaced = temp_path / "incoming-replaced"
        mixed.mkdir()
        replaced.mkdir()
        for role in ("base", "left", "right"):
            source = ROOT / roles[role]["file"]
            shutil.copy2(source, mixed / source.name)
            shutil.copy2(source, replaced / source.name)
        shutil.copy2(impostor, mixed / "MIXED-IN-stress-left.sqlite")
        shutil.copy2(impostor, replaced / Path(roles["left"]["file"]).name)

        mixed_report = scan_source_dir(mixed, cert)
        replaced_report = scan_source_dir(replaced, cert)
        mixed_report["directory"] = "<temporary-directory>/incoming-mixed"
        replaced_report["directory"] = "<temporary-directory>/incoming-replaced"
        result = {
            "scenario_extra_same_schema_file": mixed_report,
            "scenario_left_role_replaced_by_same_schema_file": replaced_report,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        detected_extra = mixed_report["mixed_in_same_schema"] and not mixed_report["ok"]
        detected_replace = "left" in replaced_report["missing_roles"] and replaced_report["mixed_in_same_schema"]
        print(f"\nMIXUP_SELF_TEST={'PASS' if detected_extra and detected_replace else 'FAIL'}")
        return detected_extra and detected_replace


def print_report(checks, source_report=None, business=None) -> bool:
    print("Pagefold r059 离线交接包核对")
    print(f"root: {ROOT}")
    failed = 0
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{mark}] {name} — {detail}")
    if source_report is not None:
        print("\n来源目录按 SHA-256 角色识别；同模式但摘要不符会标为 foreign_same_schema。")
        for record in source_report["records"]:
            print(f"  - {record['file']}: {record['classification']}")
    if business is not None:
        blob = bytes(business["north"]["payload"])
        print("\n封存结果抽查：")
        print("  SELECT ... FROM node WHERE bench='north' AND slot=1;")
        print(f"  note={business['north']['note']!r}; payload=hex:{blob.hex()}; weight={business['north']['weight']}")
        print(f"  node keys={business['keys']}")
    print(f"\n结论: {'PASS — 交接包完整且凭证/业务证据一致' if failed == 0 else f'FAIL — {failed} 项未通过'}")
    return failed == 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Pagefold offline handoff or scan a source directory.")
    parser.add_argument("--scan-dir", type=Path, help="scan an incoming source directory for mixed SQLite files")
    parser.add_argument("--mixup-self-test", action="store_true", help="prove detection using the bundled same-schema impostor")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.mixup_self_test:
        return 0 if mixup_self_test() else 1
    if args.scan_dir:
        cert = load_json(CERT_PATH)
        report = scan_source_dir(args.scan_dir.resolve(), cert)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 3

    checks: list[tuple[str, bool, str]] = []
    manifest = verify_manifest(checks)
    cert, roles = load_trust_inputs(checks)
    result_path = None
    source_report = None
    business = None
    if cert is not None and roles is not None:
        result_path = verify_sources_and_certificate(checks, cert, roles)
        verify_state_backup(checks, cert)
        source_report = verify_package_source_scan(checks, cert)
        if result_path is not None:
            business = verify_database_business_evidence(checks, result_path)
    ok = print_report(checks, source_report, business)
    if manifest is None:
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
