#!/usr/bin/env python3
"""Generate SHA256SUMS and handoff-manifest.json for this package.

This is a build/reproducibility tool.  Receivers normally only need
verify_offline.py; running this again recomputes the local inventory.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "handoff-manifest.json"
SUMS = ROOT / "SHA256SUMS"
CERT = ROOT / "certificate" / "pagefold-certificate.json"
ROLES = ROOT / "sources" / "SOURCE-ROLES.json"
BUILD_INFO = ROOT / "audit" / "build-info.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rel_files() -> list[str]:
    names = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel == MANIFEST.name or rel == SUMS.name:
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        names.append(rel)
    return sorted(names)


def main() -> None:
    cert = json.loads(CERT.read_text(encoding="utf-8"))
    roles_doc = json.loads(ROLES.read_text(encoding="utf-8"))
    build_info = json.loads(BUILD_INFO.read_text(encoding="utf-8"))

    names = rel_files()
    sum_lines = [f"{sha256_file(ROOT / name)}  {name}" for name in names]
    # Standard checksum file does not try to pin itself or the manifest.
    SUMS.write_text("\n".join(sum_lines) + "\n", encoding="utf-8")

    file_records = []
    for name in sorted(names + [SUMS.name]):
        path = ROOT / name
        file_records.append({"path": name, "sha256": sha256_file(path), "size": path.stat().st_size})

    result_name = "result/merged-r059-right-choice.sqlite"
    manifest = {
        "format": "pagefold-offline-handoff-manifest-v1",
        "manifest_generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_build_generated_at_utc": build_info["generated_at_utc"],
        "repository_commit": build_info["repository_commit"],
        "repository_branch": build_info["repository_branch"],
        "plan_id": cert["plan"],
        "revision": cert["revision"],
        "schema_signature": cert["schema"],
        "certificate": {
            "path": "certificate/pagefold-certificate.json",
            "sha256": sha256_file(CERT),
        },
        "result": {
            "path": result_name,
            "sha256": cert["candidate_digest"],
            "size": cert["candidate_size"],
        },
        "source_roles": roles_doc["roles"],
        "mixup_detection": {
            "self_test_command": "python3 tools/verify_offline.py --mixup-self-test",
            "scan_command": "python3 tools/verify_offline.py --scan-dir /path/to/source-mirror",
            "fixture": "negative-tests/stress-left-same-schema.sqlite",
            "self_test_record": "negative-tests/mixup-selftest.txt",
            "package_rejection_record": "negative-tests/package-mixup-detection.txt",
            "principle": "Same SQLite schema is not identity; role files are byte-SHA256 pinned to the certificate.",
        },
        "files": file_records,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(MANIFEST)
    print(SUMS)
    print(f"files={len(file_records)}")


if __name__ == "__main__":
    main()
