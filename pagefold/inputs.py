import json
import re
from email.parser import BytesParser
from email.policy import default
from urllib.parse import parse_qs

from .errors import Problem

TOKEN = re.compile(r"[a-f0-9]{64}\Z")


def fields(handler, limit):
    data = payload(handler, limit)
    nature = handler.headers.get_content_type()
    try:
        if nature == "application/json":
            result = json.loads(data)
        elif nature == "application/x-www-form-urlencoded":
            result = {key: values[-1] for key, values in parse_qs(
                data.decode("utf-8"), keep_blank_values=True, max_num_fields=20).items()}
        else:
            raise Problem("media_type", "请提交 JSON 或网页表单。", 415)
    except (ValueError, UnicodeError) as exc:
        raise Problem("body", "请求内容无法读取。", 400) from exc
    if not isinstance(result, dict):
        raise Problem("body", "请求须包含字段对象。", 400)
    return result


def payload(handler, limit):
    lengths = handler.headers.get_all("Content-Length", [])
    if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
        raise Problem("content_length", "请求须提供有效的内容长度。", 411)
    size = int(lengths[0])
    if size > limit:
        raise Problem("request_size", "请求超过允许大小。", 413)
    data = handler.rfile.read(size)
    if len(data) != size:
        raise Problem("request_incomplete", "请求内容未完整传输。", 400)
    return data


def unsigned(value, name):
    if type(value) is int and 0 <= value <= 2**53 - 1:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,15}", value):
        return int(value)
    raise Problem("number", f"字段 {name} 须为非负整数。", 400)


def token(value, name):
    if isinstance(value, str) and TOKEN.fullmatch(value):
        return value
    raise Problem("token", f"字段 {name} 须为 64 位十六进制摘要。", 400)


def multipart(handler, limit):
    """解析 multipart/form-data，返回 (字段名, 文件名, 字节) 列表。

    只服务于镜像组登记：每个部分须是带文件名的文件字段，且不接受嵌套内容。
    """
    content_type = handler.headers.get("Content-Type", "")
    if "boundary=" not in content_type:
        raise Problem("media_type", "multipart 请求缺少 boundary。", 400)
    data = payload(handler, limit)
    try:
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + content_type.encode("iso-8859-1") + b"\r\n\r\n" + data)
        parts = list(message.iter_parts())
    except (ValueError, TypeError, UnicodeError) as exc:
        raise Problem("body", "multipart 请求内容无法读取。", 400) from exc
    if not parts or any(part.is_multipart() for part in parts):
        raise Problem("body", "组登记只接受单层文件字段。", 400)
    result = []
    for part in parts:
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        if not name or filename is None:
            raise Problem("body", "组登记的每个部分都须是带文件名的文件字段。", 400)
        payload_bytes = part.get_payload(decode=True)
        if payload_bytes is None:
            raise Problem("body", "multipart 部分无法解码。", 400)
        result.append((name, filename, payload_bytes))
    return result
