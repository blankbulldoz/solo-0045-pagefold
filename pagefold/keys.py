"""Typed cells, keys and rows as they leave SQLite.

The wire form (``["t", "north"]``) is a transport encoding owned by
``pagefold.codec``.  Inside the application a :class:`Value` carries the same
type tag and canonical payload, but identity is a value object instead of a
JSON string.  Conversions happen only in the constructors and in
``Value.atom``/``Value.sqlite``; see ``docs/typed-keys.md``.
"""

from dataclasses import dataclass

from .codec import atom, canonical, fingerprint, unpack
from .errors import Problem

KINDS = ("n", "i", "r", "t", "b")


@dataclass(frozen=True, eq=False)
class Value:
    """One SQLite cell with its storage class preserved.

    ``payload`` keeps the same canonical text the wire atom uses (decimal for
    integers, hexadecimal for reals, base64 for blobs).  Keeping the payload
    rather than the live Python value means value equality matches wire
    equality exactly, including the ``0.0``/``-0.0`` distinction.
    """

    kind: str
    payload: str | None = None

    @classmethod
    def of(cls, raw):
        """Classify a value returned by SQLite using the wire codec."""
        encoded = atom(raw)
        return cls(encoded[0], encoded[1] if len(encoded) > 1 else None)

    @classmethod
    def key_of(cls, raw):
        """Classify a primary-key cell, folding exact integral reals to integers.

        SQLite compares INTEGER and REAL numerically in a key, so ``1`` and
        ``1.0`` must identify the same row while the stored cell keeps its own
        type tag.  This mirrors ``pagefold.rows.key_atom`` from before the
        refactor.
        """
        if isinstance(raw, float) and raw.is_integer() and -(2**63) <= raw < 2**63:
            raw = int(raw)
        return cls.of(raw)

    @classmethod
    def from_atom(cls, value):
        """Rebuild a cell from its wire atom (plan and catalog boundaries)."""
        if (not isinstance(value, (list, tuple)) or not value or value[0] not in KINDS
                or len(value) != (1 if value[0] == "n" else 2)
                or (value[0] != "n" and not isinstance(value[1], str))):
            raise Problem("unsupported_value", "镜像包含无法保真编码的值。")
        return cls(value[0], value[1] if len(value) > 1 else None)

    def atom(self):
        """Return the wire atom for persistence and HTTP output."""
        return [self.kind] if self.kind == "n" else [self.kind, self.payload]

    def sqlite(self):
        """Return the Python value to bind in a SQLite statement."""
        return unpack(self.atom())

    def __eq__(self, other):
        return (isinstance(other, Value) and self.kind == other.kind
                and self.payload == other.payload)

    def __hash__(self):
        return hash((self.kind, self.payload))


@dataclass(frozen=True)
class Key:
    """A primary key of typed cells plus its stable wire token.

    ``token`` is the canonical serialization of the wire atoms.  It is the
    identity used for duplicate detection and the sort order used by row
    windows, exactly as before the refactor, but it is derived data rather
    than the representation itself.
    """

    values: tuple[Value, ...]
    token: str

    @classmethod
    def of(cls, raw_values):
        """Build a key from SQLite column values, normalizing numeric identity."""
        values = tuple(Value.key_of(value) for value in raw_values)
        return cls._with_token(values)

    @classmethod
    def from_atoms(cls, atoms):
        """Build a key from the wire atoms stored in a saved plan."""
        return cls._with_token(tuple(Value.from_atom(value) for value in atoms))

    @classmethod
    def _with_token(cls, values):
        return cls(values, canonical([value.atom() for value in values]))

    def atoms(self):
        return [value.atom() for value in self.values]

    def item_id(self, table):
        """Stable plan-item identifier for this table and key."""
        return fingerprint([table, self.atoms()])

    @property
    def has_null(self):
        return any(value.kind == "n" for value in self.values)

    def __hash__(self):
        return hash(self.token)

    def __eq__(self, other):
        return isinstance(other, Key) and self.token == other.token


@dataclass(frozen=True)
class Row:
    """All cells of one row with their type tags preserved."""

    values: tuple[Value, ...]

    @classmethod
    def of(cls, raw_values):
        return cls(tuple(Value.of(value) for value in raw_values))

    @classmethod
    def from_atoms(cls, atoms):
        return cls(tuple(Value.from_atom(value) for value in atoms))

    def atoms(self):
        return [value.atom() for value in self.values]

    def __eq__(self, other):
        return isinstance(other, Row) and self.values == other.values

    def __hash__(self):
        return hash(self.values)
