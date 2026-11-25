"""Build the two-database reporting demonstration from real Pagefold merges.

The same three SQLite images are merged twice.  One plan resolves every
conflict to the left branch, the other resolves every conflict to the right
branch.  Both plans are rehearsed, sealed and downloaded through the public
HTTP API.  The resulting candidates, their certificates and the source images
are written to the delivery directory.

Run:

    python3 -m examples.reporting_pair [output-directory]
"""

import argparse
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pagefold.server import make_server
from pagefold.store import Store

SCHEMA = """
CREATE TABLE bench(
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE node(
    bench TEXT NOT NULL REFERENCES bench(code),
    slot INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY(bench, slot)
) WITHOUT ROWID;
CREATE TABLE binding(
    id INTEGER PRIMARY KEY,
    bench TEXT NOT NULL,
    slot INTEGER NOT NULL,
    channel TEXT NOT NULL,
    FOREIGN KEY(bench, slot) REFERENCES node(bench, slot)
);
"""

BENCHES = [("north", "北港"), ("south", "南港")]
NODES = [
    ("north", 1, "北一泊位", "steel"),
    ("north", 2, "北二泊位", "steel"),
    ("south", 1, "南一浮标", "buoy"),
    ("south", 2, "南二浮标", "buoy"),
]
# Binding 30 never changes, so north/2 keeps exactly one binding in both outcomes.
# Bindings 10 and 20 change in both branches, producing the same two conflict
# items in both plans; only the decision differs.
BASE_BINDINGS = [
    (10, "north", 1, "primary"),
    (20, "south", 2, "backup"),
    (30, "north", 2, "steady"),
]

# Left: both moved bindings pile onto south/2, leaving north/1 and south/1 unbound.
LEFT_MOVES = [
    ("UPDATE binding SET bench='south', slot=2, channel='primary' WHERE id=10", ()),
    ("UPDATE binding SET channel='overflow' WHERE id=20", ()),
]
# Right: both moved bindings pile onto north/1, leaving south/1 and south/2 unbound.
RIGHT_MOVES = [
    ("UPDATE binding SET channel='direct' WHERE id=10", ()),
    ("UPDATE binding SET bench='north', slot=1 WHERE id=20", ()),
]

LABELS = {"base": "reporting base", "left": "reporting left", "right": "reporting right"}


def image(moves=()):
    """Create one self-contained source image with the shared schema."""
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO bench(code, label) VALUES(?,?)", BENCHES)
        db.executemany("INSERT INTO node(bench, slot, name, kind) VALUES(?,?,?,?)", NODES)
        db.executemany("INSERT INTO binding(id, bench, slot, channel) VALUES(?,?,?,?)", BASE_BINDINGS)
        for statement, parameters in moves:
            db.execute(statement, parameters)
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("source image violates a foreign key")
        db.commit()
        return db.serialize()
    finally:
        db.close()


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200, headers=None):
        request_headers = dict(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=data, headers=request_headers)
        try:
            with urlopen(request, timeout=15) as result:
                body, actual = result.read(), result.status
                kind = result.headers.get_content_type()
        except HTTPError as error:
            with error:
                body, actual = error.read(), error.code
                kind = error.headers.get_content_type()
        if actual != status:
            raise RuntimeError(f"{route}: {actual} != {status}: {body[:400]!r}")
        return json.loads(body) if kind == "application/json" else body

    def upload(self, label, data):
        return self.call("/api/images", data, 201, {
            "X-Image-Name": quote(label, safe=""),
            "Content-Type": "application/octet-stream",
        })


def merge(client, sources, role):
    """Create, decide, rehearse, seal and download one complete outcome."""
    plan = client.call("/api/plans", sources, 201)
    conflicts = [item for item in plan["items"] if not item["automatic"]]
    if len(conflicts) != 2:
        raise RuntimeError(f"expected two binding conflicts, got {len(conflicts)}")
    revision = plan["revision"]
    for item in conflicts:
        result = client.call(f"/api/plans/{plan['id']}/decisions", {
            "revision": revision, "item": item["id"], "choice": role})
        revision = result["revision"]
    attempt = client.call(f"/api/plans/{plan['id']}/rehearse", {"revision": revision})
    if attempt["status"] != "passed":
        raise RuntimeError(f"{role} rehearsal failed: {attempt}")
    certificate = client.call(f"/api/plans/{plan['id']}/seal", {"revision": revision})
    candidate = client.call(f"/api/plans/{plan['id']}/download")
    actual = hashlib.sha256(candidate).hexdigest()
    if actual != certificate["candidate_digest"]:
        raise RuntimeError(f"{role} download digest does not match certificate")
    return {"plan": plan["id"], "revision": revision, "certificate": certificate,
            "candidate": candidate, "conflicts": conflicts}


def counts(connection):
    """Return the invariant business counts and the per-key detail."""
    per_key = [
        {"bench": bench, "slot": slot, "bindings": bindings}
        for bench, slot, bindings in connection.execute("""
            SELECT n.bench, n.slot, COUNT(b.id) AS bindings
            FROM node n
            LEFT JOIN binding b ON b.bench = n.bench AND b.slot = n.slot
            GROUP BY n.bench, n.slot
            ORDER BY n.bench, n.slot
        """)
    ]
    histogram = {}
    for row in per_key:
        histogram[str(row["bindings"])] = histogram.get(str(row["bindings"]), 0) + 1
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


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build(output):
    sources_dir = output / "sources"
    outcomes_dir = output / "outcomes"
    sources_dir.mkdir(parents=True, exist_ok=True)
    outcomes_dir.mkdir(parents=True, exist_ok=True)

    images = {"base": image(), "left": image(LEFT_MOVES), "right": image(RIGHT_MOVES)}
    for role, data in images.items():
        (sources_dir / f"{role}.sqlite").write_bytes(data)

    with tempfile.TemporaryDirectory(prefix="pagefold-reporting-pair-") as root:
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(server.server_port)
                ids = {role: client.upload(LABELS[role], data)["id"] for role, data in images.items()}
                # Both plans consume exactly the same three registered images.
                left = merge(client, ids, "left")
                right = merge(client, ids, "right")
            finally:
                server.shutdown()
                thread.join()

    report = {}
    for role, data in (("left", left), ("right", right)):
        stem = f"decision-{role}"
        (outcomes_dir / f"{stem}.sqlite").write_bytes(data["candidate"])
        write_json(outcomes_dir / f"{stem}.certificate.json", data["certificate"])
        with sqlite3.connect(outcomes_dir / f"{stem}.sqlite") as db:
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError(f"{stem} contains a foreign key violation")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError(f"{stem} failed SQLite integrity_check")
            report[role] = {
                "file": f"outcomes/{stem}.sqlite",
                "certificate": f"outcomes/{stem}.certificate.json",
                "plan": data["plan"],
                "revision": data["revision"],
                "candidate_digest": data["certificate"]["candidate_digest"],
                "candidate_size": data["certificate"]["candidate_size"],
                "counts": counts(db),
            }

    left_counts, right_counts = report["left"]["counts"], report["right"]["counts"]
    invariant = ("nodes", "bindings", "bound_nodes", "unbound_nodes",
                 "multi_bound_nodes", "anomalous_nodes", "histogram")
    if any(left_counts[name] != right_counts[name] for name in invariant):
        raise RuntimeError("the designed cross-table invariant does not hold")
    if (left_counts["unbound_nodes"], left_counts["multi_bound_nodes"]) != (2, 1):
        raise RuntimeError("the source images no longer produce the documented counts")
    keys = {(row["bench"], row["slot"]): row["bindings"] for row in left_counts["per_key"]}
    other = {(row["bench"], row["slot"]): row["bindings"] for row in right_counts["per_key"]}
    if set(keys) != set(other):
        raise RuntimeError("the two outcomes no longer share the same composite key set")
    differences = [{"bench": bench, "slot": slot, "left": keys[(bench, slot)],
                    "right": other[(bench, slot)]}
                   for bench, slot in sorted(keys) if keys[(bench, slot)] != other[(bench, slot)]]
    if differences != [{"bench": "north", "slot": 1, "left": 0, "right": 2},
                       {"bench": "south", "slot": 2, "left": 2, "right": 0}]:
        raise RuntimeError(f"unexpected per-key differences: {differences}")

    manifest = {
        "format": "pagefold-reporting-pair-v1",
        "description": "Same source images, opposite decisions, equal counts at different composite keys.",
        "sources": {role: {"file": f"sources/{role}.sqlite", "label": LABELS[role],
                           "digest": hashlib.sha256(data).hexdigest()}
                    for role, data in images.items()},
        "outcomes": report,
        "invariant": {name: left_counts[name] for name in invariant},
        "key_differences": differences,
    }
    write_json(output / "manifest.json", manifest)
    for role in ("left", "right"):
        summary = report[role]["counts"]
        print(f"{role:5} {report[role]['candidate_digest'][:16]}…  "
              f"unbound={summary['unbound_nodes']} multi={summary['multi_bound_nodes']} "
              f"histogram={summary['histogram']}")
    print(f"wrote {output}")
    return manifest


def main():
    parser = argparse.ArgumentParser(description="构建报表演示双库")
    parser.add_argument("output", nargs="?", type=Path,
                        default=Path(__file__).resolve().parents[1] / "deliverables" / "reporting-pair")
    build(parser.parse_args().output)


if __name__ == "__main__":
    main()
