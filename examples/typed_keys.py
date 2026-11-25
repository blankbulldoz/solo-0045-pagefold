"""Capture a comparable snapshot of rows, plans and sealed content.

The script only uses the public HTTP entry points and its own image builder, so
the same file can be run against a pre-refactor checkout and the current tree:

    PYTHONPATH=/tmp/pagefold-before python3 examples/typed_keys.py --output before.json
    python3 examples/typed_keys.py --output after.json

The resulting JSON deliberately keeps item identifiers, key atoms, row atoms,
offset windows, the certificate and the candidate bytes digest so the two runs
can be diffed directly.
"""

import argparse
import base64
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pagefold.server import make_server
from pagefold.store import Store

SCHEMA = """
CREATE TABLE mix(
    tag TEXT NOT NULL,
    ident BLOB NOT NULL,
    note TEXT,
    payload BLOB,
    gravity REAL,
    PRIMARY KEY(tag, ident)
) WITHOUT ROWID;
CREATE TABLE parent(
    code TEXT NOT NULL,
    slot INTEGER NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY(code, slot)
) WITHOUT ROWID;
CREATE TABLE child(
    id INTEGER PRIMARY KEY,
    code TEXT,
    slot INTEGER,
    FOREIGN KEY(code, slot) REFERENCES parent(code, slot)
);
CREATE TABLE ord(
    n INTEGER PRIMARY KEY,
    label TEXT NOT NULL
) WITHOUT ROWID;
"""


def atom(value):
    if value is None:
        return ["n"]
    if isinstance(value, int):
        return ["i", str(value)]
    if isinstance(value, float):
        return ["r", value.hex()]
    if isinstance(value, str):
        return ["t", value]
    if isinstance(value, bytes):
        return ["b", base64.b64encode(value).decode("ascii")]
    raise TypeError(value)


def image(flavor):
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO mix VALUES(?,?,?,?,?)", [
            ("num", 1, "base-num", b"\x01", 1.5),
            ("empty", b"", "base-empty", b"x", None),
            ("blobkey", b"\x00\xff", "base-blob", b"\x00", 0.0),
            ("textkey", "same", "base-text", None, 2.0),
            ("keep", "k", "base-keep", b"", None),
        ])
        db.executemany("INSERT INTO parent VALUES(?,?,?)", [
            ("north", 1, "base-north"), ("south", 1, "base-south")])
        db.execute("INSERT INTO child VALUES(1,'north',1)")
        db.executemany("INSERT INTO ord VALUES(?,?)",
                       [(n, f"row-{n:02d}") for n in range(1, 66)])
        if flavor == "left":
            # Same numeric key identity as INTEGER 1, but stored as REAL.
            db.execute("UPDATE mix SET ident=1.0, note='left-num' WHERE tag='num'")
            db.execute("UPDATE mix SET gravity=1.5 WHERE tag='blobkey'")
            db.execute("DELETE FROM mix WHERE tag='textkey'")
            db.execute("INSERT INTO mix VALUES('fresh',x'8000','left-new',x'',3.25)")
            db.execute("UPDATE parent SET label='left-north' WHERE code='north'")
            db.execute("UPDATE ord SET label='left-03' WHERE n=3")
        elif flavor == "right":
            db.execute("UPDATE mix SET payload=x'' WHERE tag='empty'")
            db.execute("UPDATE mix SET gravity=2.5 WHERE tag='blobkey'")
            db.execute("UPDATE parent SET label='right-south' WHERE code='south'")
            db.execute("UPDATE ord SET label='right-60' WHERE n=60")
        db.commit()
        return db.serialize()
    finally:
        db.close()


def table_dump(db, table):
    columns = [row[1] for row in db.execute(f"PRAGMA main.table_xinfo({quote(table)})")
               if row[1]]
    rows = [[atom(value) for value in row]
            for row in db.execute(f"SELECT * FROM main.{quote(table)}")]
    rows.sort(key=lambda row: json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return {"columns": columns, "rows": rows}


def candidate_dump(data):
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(data)
        schema = [[kind, name, target, sql] for kind, name, target, sql in db.execute(
            "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
            "WHERE substr(name,1,7) <> 'sqlite_' ORDER BY type,name")]
        tables = [name for kind, name, _, _ in schema if kind == "table"]
        return {"schema": schema, "tables": {name: table_dump(db, name) for name in tables},
                "integrity": db.execute("PRAGMA main.integrity_check(1)").fetchone()[0],
                "foreign_keys": [list(row) for row in db.execute("PRAGMA main.foreign_key_check")]}
    finally:
        db.close()


def quote(name):
    return '"' + name.replace('"', '""') + '"'


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
            with urlopen(request, timeout=20) as result:
                body, actual, kind = result.read(), result.status, result.headers.get_content_type()
        except HTTPError as error:
            with error:
                body, actual, kind = error.read(), error.code, error.headers.get_content_type()
        if actual != status:
            raise RuntimeError(f"{route}: {actual} != {status}: {body[:400]}")
        return json.loads(body) if kind == "application/json" else body

    def upload(self, name, data):
        return self.call("/api/images", data, 201, {
            "X-Image-Name": name, "Content-Type": "application/octet-stream"})


def capture(client):
    images = {role: client.upload(role, image(role)) for role in ("base", "left", "right")}
    catalogs = {}
    windows = {}
    pages = {}
    for role, record in images.items():
        catalog = client.call(f'/api/images/{record["id"]}')["catalog"]
        catalogs[role] = catalog
        windows[role] = {}
        for table in catalog["tables"]:
            name = table["name"]
            windows[role][name] = {}
            offset = 0
            while True:
                route = f'/api/images/{record["id"]}/rows?' + urlencode({"table": name, "offset": offset})
                windows[role][name][str(offset)] = client.call(route)
                total = windows[role][name][str(offset)]["total"]
                if offset + 50 >= total:
                    break
                offset += 50
            page = client.call(f'/images/{record["id"]}/rows?' + urlencode({"table": name}))
            pages[f"{role}:{name}"] = hashlib.sha256(page).hexdigest()
        pages[f"{role}:catalog"] = hashlib.sha256(
            client.call(f'/images/{record["id"]}')).hexdigest()

    sources = {role: record["id"] for role, record in images.items()}
    plan = client.call("/api/plans", sources, 201)
    plan_id = plan["id"]
    route = f"/api/plans/{plan_id}"
    created = client.call(route)
    decisions = []
    for item in created["items"]:
        if item["automatic"] is not None:
            continue
        answer = client.call(route + "/decisions",
                             {"item": item["id"], "choice": "right", "revision": created["revision"]})
        decisions.append(answer)
        created = client.call(route)
    after = client.call(route)
    rehearsal = client.call(route + "/rehearse", {"revision": after["revision"]})
    attempts = client.call(route + "/attempts")
    certificate = client.call(route + "/seal", {"revision": after["revision"]})
    candidate = client.call(route + "/download")
    sealed = {
        "rehearsal": rehearsal,
        "attempts": attempts,
        "certificate": certificate,
        "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
        "candidate_size": len(candidate),
        "candidate": candidate_dump(candidate),
    }
    captured = {
        "image_ids": sources,
        "catalogs": catalogs,
        "rows": windows,
        "pages": pages,
        "plan": {"created": plan, "decisions": decisions, "after": after},
        "sealed": sealed,
    }
    text = json.dumps(captured, ensure_ascii=True, sort_keys=True, indent=1)
    return json.loads(text.replace(plan_id, "<plan>"))


def run():
    with tempfile.TemporaryDirectory(prefix="pagefold-typed-keys-") as root:
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                return capture(Client(server.server_port))
            finally:
                server.shutdown()
                thread.join()


def main():
    parser = argparse.ArgumentParser(description="记录镜像行、计划与封存内容的对照快照")
    parser.add_argument("--output", type=Path, required=True)
    result = run()
    parser.parse_args().output.write_text(
        json.dumps(result, ensure_ascii=True, sort_keys=True, indent=1) + "\n", encoding="ascii")
    print(f"写入 {parser.parse_args().output}")


if __name__ == "__main__":
    main()
