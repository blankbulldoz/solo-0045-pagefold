import json

from .codec import fingerprint


ROLES = ("base", "left", "right")


def calculate(base, left, right):
    result = []
    for table in sorted(base):
        versions = [snapshot[table] for snapshot in (base, left, right)]
        keys = sorted(set().union(*versions))
        for key in keys:
            ancestor, lrow, rrow = (values.get(key) for values in versions)
            if lrow == rrow == ancestor:
                continue
            choice = "left" if lrow == rrow or rrow == ancestor else "right" if lrow == ancestor else None
            item_key = json.loads(key)
            result.append({"id": fingerprint([table, item_key]), "table": table, "key": item_key,
                           "base": ancestor, "left": lrow, "right": rrow, "automatic": choice})
    return result


def selected(item, decisions):
    role = item["automatic"] or decisions.get(item["id"])
    return (role, item[role] if role else None)
