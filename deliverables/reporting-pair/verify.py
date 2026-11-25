"""Standalone verification for the reporting demonstration pair.

Needs only Python 3 with the standard sqlite3 module; Pagefold is not
imported or started.  Run from anywhere:

    python3 verify.py

It checks four things:

1. the source images and both outcome files match the digests recorded in
   manifest.json and in the two Pagefold certificates;
2. both outcomes pass SQLite integrity and foreign-key checks;
3. the cross-table business counts are identical in both outcomes;
4. the per-composite-key binding detail differs exactly where expected.

Exit status is 0 when every check passes and 1 otherwise.
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

EXPECTED_INVARIANT = {
    "nodes": 4,
    "bindings": 3,
    "bound_nodes": 2,
    "unbound_nodes": 2,
    "multi_bound_nodes": 1,
    "anomalous_nodes": 3,
    "histogram": {"0": 2, "1": 1, "2": 1},
}
EXPECTED_DIFFERENCES = [
    {"bench": "north", "slot": 1, "left": 0, "right": 2},
    {"bench": "south", "slot": 2, "left": 2, "right": 0},
]
EXPECTED_SAME_KEYS = [
    {"bench": "north", "slot": 2, "left": 1, "right": 1},
    {"bench": "south", "slot": 1, "left": 0, "right": 0},
]

failures = []


def check(condition, description):
    print(("PASS  " if condition else "FAIL  ") + description)
    if not condition:
        failures.append(description)


def digest(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def counts(connection):
    per_key = [
        {"bench": bench, "slot": slot, "bindings": bindings}
        for bench, slot, bindings in connection.execute("""
            SELECT n.bench, n.slot, COUNT(b.id) AS bindings
            FROM node AS n
            LEFT JOIN binding AS b
                   ON b.bench = n.bench AND b.slot = n.slot
            GROUP BY n.bench, n.slot
            ORDER BY n.bench, n.slot
        """)
    ]
    histogram = {}
    for row in per_key:
        key = str(row["bindings"])
        histogram[key] = histogram.get(key, 0) + 1
    return {
        "nodes": len(per_key),
        "bindings": connection.execute("SELECT COUNT(*) FROM binding").fetchone()[0],
        "bound_nodes": sum(row["bindings"] > 0 for row in per_key),
        "unbound_nodes": sum(row["bindings"] == 0 for row in per_key),
        "multi_bound_nodes": sum(row["bindings"] > 1 for row in per_key),
        "anomalous_nodes": sum(row["bindings"] != 1 for row in per_key),
        "histogram": histogram,
        "per_key": per_key,
    }


def main():
    manifest = load(ROOT / "manifest.json")
    certificates = {role: load(ROOT / entry["certificate"])
                    for role, entry in manifest["outcomes"].items()}

    for role, entry in manifest["sources"].items():
        actual = digest(ROOT / entry["file"])
        check(actual == entry["digest"], f"source {role} matches recorded digest")
        check(all(certificate["sources"][role] == actual
                  for certificate in certificates.values()),
              f"source {role} digest is bound by both certificates")

    check(certificates["left"]["sources"] == certificates["right"]["sources"],
          "both certificates name the exact same three source images")
    check(certificates["left"]["schema"] == certificates["right"]["schema"],
          "both certificates share one schema digest")
    check(certificates["left"]["plan"] != certificates["right"]["plan"],
          "the two outcomes come from different plans")
    check(certificates["left"]["decision_digest"] != certificates["right"]["decision_digest"],
          "the two plans record different decisions")
    check(certificates["left"]["candidate_digest"] != certificates["right"]["candidate_digest"],
          "the two candidates are byte-distinct")

    results = {}
    for role, entry in manifest["outcomes"].items():
        path = ROOT / entry["file"]
        actual = digest(path)
        check(actual == entry["candidate_digest"], f"{role} outcome file matches manifest digest")
        check(actual == certificates[role]["candidate_digest"],
              f"{role} outcome file matches its certificate candidate_digest")
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            check(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                  f"{role} outcome passes PRAGMA integrity_check")
            check(db.execute("PRAGMA foreign_key_check").fetchall() == [],
                  f"{role} outcome passes PRAGMA foreign_key_check")
            results[role] = counts(db)

    invariant = ("nodes", "bindings", "bound_nodes", "unbound_nodes",
                 "multi_bound_nodes", "anomalous_nodes", "histogram")
    for name in invariant:
        check(results["left"][name] == results["right"][name],
              f"count '{name}' is identical in both outcomes")
    check({k: results["left"][k] for k in invariant} == EXPECTED_INVARIANT,
          "the shared counts equal the designed demo values")
    check(manifest["invariant"] == EXPECTED_INVARIANT,
          "manifest records the designed shared counts")
    check(manifest["key_differences"] == EXPECTED_DIFFERENCES,
          "manifest records the designed per-key differences")
    for role, entry in manifest["outcomes"].items():
        check(entry["counts"]["histogram"] == EXPECTED_INVARIANT["histogram"],
              f"manifest {role} histogram matches the shared histogram")

    left = {(row["bench"], row["slot"]): row["bindings"] for row in results["left"]["per_key"]}
    right = {(row["bench"], row["slot"]): row["bindings"] for row in results["right"]["per_key"]}
    check(set(left) == set(right), "both outcomes contain the same set of composite keys")
    differences = [{"bench": bench, "slot": slot, "left": left[(bench, slot)],
                    "right": right[(bench, slot)]}
                   for bench, slot in sorted(set(left) | set(right))
                   if left.get((bench, slot)) != right.get((bench, slot))]
    check(differences == EXPECTED_DIFFERENCES,
          "unbound/multi-bound detail differs at exactly north/1 and south/2")
    same_keys = [{"bench": bench, "slot": slot, "left": left[(bench, slot)],
                  "right": right[(bench, slot)]}
                 for bench, slot in sorted(set(left) & set(right))
                 if left[(bench, slot)] == right[(bench, slot)]]
    check(same_keys == EXPECTED_SAME_KEYS,
          "north/2 (1 binding) and south/1 (0 bindings) are untouched, proving not all keys moved")

    if failures:
        print(f"\n{len(failures)} check(s) failed.")
        return 1
    print("\nAll checks passed: equal counts, different keys, both lineage records intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
