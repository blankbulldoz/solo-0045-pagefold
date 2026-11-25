"""Relation structure with controlled column and key identity.

``describe`` is the only place that reads SQLite's ``PRAGMA`` output.  It
returns a :class:`Catalog` of :class:`Table` and :class:`Column` value objects;
the persisted/HTTP shape is produced explicitly by :meth:`Catalog.record` and
parsed back by :meth:`Catalog.from_record`.  No other module quotes column
names or recomputes primary-key positions.
"""

from dataclasses import dataclass, replace

from .codec import fingerprint
from .errors import Problem
from .sqlite_image import ident


@dataclass(frozen=True)
class Column:
    """A column the application is allowed to name in SQL."""

    name: str
    declared_type: str | None


@dataclass(frozen=True)
class ForeignKey:
    """One declared foreign-key row, columns kept in declaration order."""

    id: int
    seq: int
    table: str
    from_column: str
    to_column: str | None
    on_update: str
    on_delete: str
    match: str

    @classmethod
    def from_row(cls, row):
        return cls(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7])

    def record(self):
        return [self.id, self.seq, self.table, self.from_column, self.to_column,
                self.on_update, self.on_delete, self.match]

    def label(self):
        return f"{self.from_column} → {self.table}.{self.to_column or '主键'}"


@dataclass(frozen=True)
class Table:
    """A plain table: ordered columns, ordered key columns, declared references."""

    name: str
    columns: tuple[Column, ...]
    key: tuple[Column, ...]
    foreign_keys: tuple[ForeignKey, ...] = ()
    rows: int | None = None

    @classmethod
    def build(cls, name, columns, key_names, foreign_keys=(), rows=None):
        known = {column.name: column for column in columns}
        if len(known) != len(columns) or any(name not in known for name in key_names):
            raise Problem("table_shape", f"表 {name} 的主键列不唯一或不存在。")
        return cls(name, tuple(columns), tuple(known[name] for name in key_names),
                   tuple(foreign_keys), rows)

    @classmethod
    def from_record(cls, record):
        columns = [Column(name, declared) for name, declared in zip(record["columns"], record["types"])]
        foreign = [ForeignKey(*row) for row in record["foreign_keys"]]
        return cls.build(record["name"], columns, record["key"], foreign, record.get("rows"))

    @property
    def column_names(self):
        return tuple(column.name for column in self.columns)

    @property
    def key_names(self):
        return tuple(column.name for column in self.key)

    def key_positions(self):
        """Ordinal positions of key columns within a full row, in key order."""
        positions = {column.name: index for index, column in enumerate(self.columns)}
        return tuple(positions[column.name] for column in self.key)

    def projection(self):
        return ",".join(ident(column.name) for column in self.columns)

    def key_predicate(self):
        return " AND ".join(f"{ident(column.name)} COLLATE BINARY=?" for column in self.key)

    def placeholders(self):
        return ",".join("?" for _ in self.columns)

    def with_rows(self, count):
        return replace(self, rows=count)

    def record(self):
        record = {"columns": [column.name for column in self.columns],
                  "foreign_keys": [foreign.record() for foreign in self.foreign_keys],
                  "key": [column.name for column in self.key],
                  "name": self.name,
                  "types": [column.declared_type for column in self.columns]}
        if self.rows is not None:
            record["rows"] = self.rows
        return record


@dataclass(frozen=True)
class Catalog:
    """The stable structure description shared by plans, pages and storage."""

    signature: str
    tables: tuple[Table, ...]

    @classmethod
    def from_record(cls, record):
        return cls(record["signature"], tuple(Table.from_record(table) for table in record["tables"]))

    def table(self, name):
        return next((table for table in self.tables if table.name == name), None)

    def with_row_counts(self, counts):
        return replace(self, tables=tuple(table.with_rows(counts[table.name]) for table in self.tables))

    def record(self):
        return {"signature": self.signature, "tables": [table.record() for table in self.tables]}


def relation(db, name):
    fields = db.execute(f"PRAGMA main.table_xinfo({ident(name)})").fetchall()
    position = sorted((field[5], field[1]) for field in fields if field[5])
    if not position or any(field[6] for field in fields):
        raise Problem("table_shape", f"表 {name} 需要显式主键，并且所有列须可写入。")
    for _, index_name, _, origin, _ in db.execute(f"PRAGMA main.index_list({ident(name)})"):
        if origin != "pk":
            continue
        parts = db.execute(f"PRAGMA main.index_xinfo({ident(index_name)})").fetchall()
        if any(part[5] and part[4].upper() != "BINARY" for part in parts):
            raise Problem("key_collation", f"表 {name} 的主键须使用 BINARY 比较。")
    foreign = [ForeignKey.from_row(row)
               for row in db.execute(f"PRAGMA main.foreign_key_list({ident(name)})")]
    columns = [Column(field[1], field[2]) for field in fields]
    return Table.build(name, columns, [column for _, column in position], foreign)


def describe(db):
    schemas = db.execute("SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
                         "WHERE substr(name,1,7) <> 'sqlite_' ORDER BY type,name").fetchall()
    listed = {r[1]: r[2] for r in db.execute("PRAGMA main.table_list") if r[0] == "main"}
    if any(kind not in {"table", "index"} or (kind == "table" and listed.get(name) != "table")
           for kind, name, _, _ in schemas):
        raise Problem("schema_objects", "镜像只接受普通表和索引。")
    tables = [(name, sql) for kind, name, _, sql in schemas if kind == "table"]
    if not 1 <= len(tables) <= 32:
        raise Problem("table_count", "镜像须包含 1 至 32 张表。")
    if db.execute("SELECT 1 FROM main.sqlite_schema WHERE name='sqlite_sequence'").fetchone():
        raise Problem("sequence", "镜像含有当前不支持的自动递增序列。")
    return Catalog(fingerprint(schemas), tuple(relation(db, name) for name, _ in tables))
