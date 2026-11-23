from functools import cache
from html import escape
from pathlib import Path
from string import Template


HERE = Path(__file__).parent
ROLE_LABELS = {"base": "祖本", "left": "左支", "right": "右支"}
STATE_LABELS = {"open": "裁决中", "sealed": "已封存", "archived": "已归档"}


def safe(value):
    return escape(str(value), quote=True)


@cache
def template(name):
    return Template((HERE / "views" / (name + ".html")).read_text(encoding="utf-8"))


def fragment(view, **values):
    return template(view).substitute(values)


def page(view, title, **values):
    return fragment("layout", title=safe(title), body=fragment(view, **values)).encode("utf-8")


def typed(atom):
    if atom[0] == "n":
        return '<span class="null">NULL</span>'
    kind = {"i": "整数", "r": "实数", "t": "文本", "b": "二进制"}[atom[0]]
    label = atom[1] if atom[0] != "b" else (atom[1] or "∅")
    return f'<span class="atom" title="{kind}">{safe(label)}</span>'


def values(row):
    if row is None:
        return '<span class="null">缺席</span>'
    return ' <span class="sep">·</span> '.join(map(typed, row))


def option(value, label, chosen=False):
    selected = " selected" if chosen else ""
    return f'<option value="{safe(value)}"{selected}>{safe(label)}</option>'
