#!/usr/bin/env python3
"""Query the sealed Pagefold result by node composite key without a server."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CERT = ROOT / "certificate" / "pagefold-certificate.json"
RESULT = ROOT / "result" / "merged-r059-right-choice.sqlite"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def uri_ro(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从已封存 SQLite 结果按 node(bench,slot) 复合键查询，并先核对凭证摘要。"
    )
    parser.add_argument("bench", nargs="?", default="north", help="bench 文本键，默认 north")
    parser.add_argument("slot", nargs="?", type=int, default=1, help="slot 整数键，默认 1")
    parser.add_argument("--json", action="store_true", help="输出 JSON（BLOB 同时给 hex/base64）")
    args = parser.parse_args()

    cert = json.loads(CERT.read_text(encoding="utf-8"))
    if not RESULT.is_file():
        print(f"missing sealed result: {RESULT}", file=sys.stderr)
        return 2
    digest = sha256_file(RESULT)
    if digest != cert["candidate_digest"]:
        print(
            "REFUSE_QUERY: result SHA-256 does not match certificate\n"
            f"  file:       {digest}\n"
            f"  certificate:{cert['candidate_digest']}",
            file=sys.stderr,
        )
        return 3

    db = sqlite3.connect(uri_ro(RESULT), uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        row = db.execute(
            "SELECT bench, slot, note, payload, weight FROM node WHERE bench=? AND slot=?",
            (args.bench, args.slot),
        ).fetchone()
    finally:
        db.close()
    if row is None:
        print(f"NOT_FOUND: node ({args.bench!r}, {args.slot})", file=sys.stderr)
        return 4

    blob = bytes(row["payload"]) if row["payload"] is not None else None
    result = {
        "authenticated_against_certificate": True,
        "candidate_digest": digest,
        "table": "node",
        "composite_key": [args.bench, args.slot],
        "row": {
            "bench": row["bench"],
            "slot": row["slot"],
            "note": row["note"],
            "payload": None if blob is None else {
                "length": len(blob),
                "hex": blob.hex(),
                "base64": base64.b64encode(blob).decode("ascii"),
                "sha256": hashlib.sha256(blob).hexdigest(),
                "python_repr": repr(blob),
            },
            "weight": row["weight"],
        },
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        p = result["row"]["payload"]
        print(f"candidate SHA-256 OK: {digest}")
        print(f"node composite key: ({row['bench']!r}, {row['slot']})")
        print(f"note   = {row['note']!r}")
        if p is None:
            print("payload = NULL")
        else:
            print(f"payload = {p['python_repr']}")
            print(f"          length={p['length']} hex={p['hex']} base64={p['base64']}")
        print(f"weight = {row['weight']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
