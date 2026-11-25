"""Three-way plan items built from typed rows.

Snapshots are ``{table: {Key: Row}}`` as produced by ``pagefold.rows``.  An
item holds :class:`~pagefold.keys.Key` and :class:`~pagefold.keys.Row`
objects; its wire record is produced only when the plan is persisted or
served.  Item identifiers, table/key ordering and the stored record shape are
unchanged from the pre-refactor format.
"""

from dataclasses import dataclass

from .keys import Key, Row


ROLES = ("base", "left", "right")


@dataclass(frozen=True)
class PlanItem:
    table: str
    key: Key
    base: Row | None
    left: Row | None
    right: Row | None
    automatic: str | None = None

    @property
    def id(self):
        return self.key.item_id(self.table)

    def row(self, role):
        return getattr(self, role) if role else None

    def record(self):
        return {"id": self.id, "table": self.table, "key": self.key.atoms(),
                "base": self.base.atoms() if self.base else None,
                "left": self.left.atoms() if self.left else None,
                "right": self.right.atoms() if self.right else None,
                "automatic": self.automatic}

    @classmethod
    def from_record(cls, record):
        rows = {role: Row.from_atoms(record[role]) if record[role] is not None else None
                for role in ROLES}
        return cls(record["table"], Key.from_atoms(record["key"]), rows["base"],
                   rows["left"], rows["right"], record["automatic"])


def calculate(base, left, right):
    result = []
    for table in sorted(base):
        versions = [snapshot[table] for snapshot in (base, left, right)]
        keys = sorted(set().union(*versions), key=lambda key: key.token)
        for key in keys:
            ancestor, lrow, rrow = (values.get(key) for values in versions)
            if lrow == rrow == ancestor:
                continue
            choice = "left" if lrow == rrow or rrow == ancestor else "right" if lrow == ancestor else None
            result.append(PlanItem(table, key, ancestor, lrow, rrow, choice))
    return result


def selected(item, decisions):
    role = item.automatic or decisions.get(item.id)
    return (role, item.row(role))
