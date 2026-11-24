from functools import cache
from html import escape
from pathlib import Path
from string import Template

from .codec import unpack


HERE = Path(__file__).parent
ROLE_LABELS = {"base": "祖本", "left": "左支", "right": "右支"}
CHOICE_LABELS = {**ROLE_LABELS, "manual": "人工合成"}
STATE_LABELS = {"open": "裁决中", "sealed": "已封存", "archived": "已归档"}
TYPE_OPTIONS = (("n", "NULL（无值）"), ("i", "整数"), ("r", "实数"),
                ("t", "文本"), ("b", "BLOB（base64）"))


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
    kind = atom[0]
    if kind == "n":
        return '<span class="null" title="NULL：没有值">NULL</span>'
    if kind == "t" and atom[1] == "":
        return '<span class="blank" title="空文本：长度为 0 的字符串，不是 NULL">空文本 &#39;&#39;</span>'
    if kind == "b":
        if not atom[1]:
            return '<span class="blank" title="空 BLOB：长度为 0 的二进制，不是 NULL">空 BLOB（0 字节）</span>'
        return f'<span class="atom" title="二进制（base64 编码）">{safe(atom[1])}</span>'
    kind_label = {"i": "整数", "r": "实数", "t": "文本"}[kind]
    return f'<span class="atom" title="{kind_label}">{safe(atom[1])}</span>'


def values(row):
    if row is None:
        return '<span class="null">缺席</span>'
    return ' <span class="sep">·</span> '.join(map(typed, row))


def option(value, label, chosen=False):
    selected = " selected" if chosen else ""
    return f'<option value="{safe(value)}"{selected}>{safe(label)}</option>'


def atom_input_value(atom):
    """把 atom 转成网页输入框里可再次转换的文本。"""
    tag = atom[0]
    if tag == "n" or len(atom) < 2 or atom[1] is None:
        return tag, ""
    if tag == "r":
        try:
            return tag, repr(unpack(atom))
        except (ValueError, TypeError):
            return tag, str(atom[1])
    return tag, str(atom[1])
