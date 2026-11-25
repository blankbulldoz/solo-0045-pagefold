"""Pagefold 的标准 WSGI 应用入口。

该模块把原来基于 ``http.server.BaseHTTPRequestHandler`` 的协议处理迁移为
PEP 3333 WSGI 应用 :func:`make_app`，业务模块（``api``、``workspace``、
``inputs``）的调用约定保持不变：:class:`Request` 暴露与旧处理器同名的
``headers``、``rfile`` 接口，因此请求体大小、媒体类型、来源校验等约束完全沿用。

模块级 :data:`application` 供 gunicorn、uWSGI、mod_wsgi 等外部 WSGI 服务器直接
引用，例如 ``gunicorn 'pagefold.wsgi:application'``。状态库路径与可信主机通过
``PAGEFOLD_STATE``、``PAGEFOLD_ALLOWED_HOSTS`` 环境变量配置，并延迟到首个请求
时初始化，避免在服务器预导入阶段打开 SQLite。
"""

import http.client
import json
import logging
import os
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import api, workspace
from .errors import Problem
from .inputs import payload
from .store import Store

ENTRY = "wsgi"
CSP = ("default-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")


class Headers:
    """仅支持读取的 WSGI 请求头视图，对齐 http.client 的取值习惯。"""

    def __init__(self, environ):
        items = []
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                items.append((key[5:].replace("_", "-").title(), value))
        if "CONTENT_TYPE" in environ:
            items.append(("Content-Type", environ["CONTENT_TYPE"]))
        if "CONTENT_LENGTH" in environ:
            items.append(("Content-Length", environ["CONTENT_LENGTH"]))
        self._items = items

    def get(self, name, default=None):
        wanted = name.lower()
        for key, value in self._items:
            if key.lower() == wanted:
                return value
        return default

    def get_all(self, name, failobj=None):
        wanted = name.lower()
        values = [value for key, value in self._items if key.lower() == wanted]
        return values or failobj

    def get_content_type(self):
        value = self.get("Content-Type", "")
        return value.split(";", 1)[0].strip().lower()


class Request:
    """把 WSGI environ 适配为旧业务代码期望的处理器接口。"""

    def __init__(self, environ):
        self.environ = environ
        self.headers = Headers(environ)
        self.rfile = environ["wsgi.input"]
        self.script = environ.get("SCRIPT_NAME", "") or ""
        self.route_path = environ.get("PATH_INFO", "/") or "/"
        query = environ.get("QUERY_STRING", "")
        self.path = self.script + self.route_path + (("?" + query) if query else "")


class Responder:
    """收集 WSGI 响应，方法名与旧 Handler 的发送方法一一对应。"""

    def __init__(self, start_response):
        self._start_response = start_response
        self.status = None
        self.headers = None

    def send_bytes(self, data, content_type, status=200, headers=()):
        self.status = status
        self.headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(data))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", CSP),
            ("X-Pagefold-Entry", ENTRY),
            *headers,
        ]
        self._start_response(
            f"{status} {http.client.responses.get(status, 'OK')}", self.headers)
        return [data] if data else []

    def json(self, result, status=200, headers=()):
        return self.send_bytes(
            json.dumps(result, ensure_ascii=True).encode("ascii"),
            "application/json; charset=utf-8", status, headers)

    def redirect(self, path):
        return self.send_bytes(b"", "text/plain", 303, [("Location", path)])

    def report(self, problem, route_path, full_path):
        if route_path.startswith("/api/"):
            return self.json(problem.as_dict(), problem.status)
        back = "/".join(full_path.split("/")[:3])
        if not (route_path.startswith("/plans/") and not full_path.startswith("//")):
            back = "/"
        return self.send_bytes(workspace.error(problem, back),
                               "text/html; charset=utf-8", problem.status)


def make_app(store, allowed_hosts=(), extra_origins=()):
    """构造标准 WSGI 应用。

    allowed_hosts 允许的 ``Host`` 头（``主机:端口``）；extra_origins 额外允许的
    浏览器 ``Origin`` 头，默认自动包含允许主机的 http/https 来源。
    """
    hosts = set(allowed_hosts)
    origins = set(extra_origins)
    origins.update(f"http://{host}" for host in hosts)
    origins.update(f"https://{host}" for host in hosts)

    def route_get(request, response, parts, query):
        path = request.route_path
        if path.startswith("/assets/"):
            body, kind = workspace.asset(path)
            return response.send_bytes(body, kind + "; charset=utf-8")
        if len(parts) > 1 and parts[0] == "api":
            result = api.read(store, parts[1:], query)
            if isinstance(result, bytes):
                return response.send_bytes(result, "application/vnd.sqlite3", headers=[
                    ("Content-Disposition", f'attachment; filename="pagefold-{parts[2]}.sqlite"')])
            headers = []
            if len(parts) == 4 and parts[3] == "certificate":
                headers.append(
                    ("Content-Disposition", f'attachment; filename="pagefold-{parts[2]}.json"'))
            return response.json(result, headers=headers)
        return response.send_bytes(workspace.workspace(store, parts, query),
                                  "text/html; charset=utf-8")

    def route_post(request, response, parts):
        origin = request.headers.get("Origin")
        if origin and origin not in origins:
            raise Problem("origin", "该请求必须来自当前工作区。", 403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise Problem("origin", "该请求必须来自当前工作区。", 403)
        path = request.route_path
        if path == "/api/images":
            return response.json(api.upload(store, request), 201)
        if path == "/demo":
            payload(request, 0)
            return response.redirect(request.script + f"/plans/{workspace.sample(store)['id']}")
        if len(parts) >= 2 and parts[:2] == ["api", "plans"]:
            return response.json(api.write_plan(store, request, parts[2:]),
                                 201 if len(parts) == 2 else 200)
        if parts and parts[0] == "plans":
            result = api.write_plan(store, request, parts[1:])
            target = parts[1] if len(parts) > 1 else result["id"]
            return response.redirect(request.script + f"/plans/{target}")
        raise Problem("route", "找不到该入口。", 404)

    def application(environ, start_response):
        request = Request(environ)
        response = Responder(start_response)
        parts = [part for part in request.route_path.split("/") if part]
        try:
            if request.headers.get("Host") not in hosts:
                raise Problem("host", "请通过本机工作区的地址访问。", 403)
            method = environ.get("REQUEST_METHOD", "")
            if method not in ("GET", "POST"):
                raise Problem("method", "该入口仅支持 GET 与 POST。", 501)
            target = urlsplit(request.path)
            if method == "POST":
                return route_post(request, response, parts)
            query = {key: values[-1] for key, values in parse_qs(
                target.query, keep_blank_values=True, max_num_fields=16).items()}
            return route_get(request, response, parts, query)
        except Problem as exc:
            return response.report(exc, request.route_path, request.path)
        except ValueError:
            return response.report(Problem("request", "请求参数无法读取。", 400),
                                   request.route_path, request.path)
        except (BrokenPipeError, ConnectionResetError):
            return []
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY:
                return response.report(Problem("state_busy", "状态库繁忙，请稍后重试。", 503),
                                       request.route_path, request.path)
            logging.exception("Request failed")
            return response.report(Problem("internal", "服务暂时无法完成请求。", 500),
                                   request.route_path, request.path)
        except Exception:
            logging.exception("Request failed")
            return response.report(Problem("internal", "服务暂时无法完成请求。", 500),
                                   request.route_path, request.path)

    return application


def _load_application():
    state = os.environ.get("PAGEFOLD_STATE", "pagefold-state.sqlite")
    hosts = [item.strip() for item in
             os.environ.get("PAGEFOLD_ALLOWED_HOSTS", "").split(",") if item.strip()]
    return make_app(Store(Path(state)), allowed_hosts=hosts)


class LazyApplication:
    """首次请求时才构造应用，兼容 gunicorn 多 worker 与预加载场景。"""

    def __init__(self):
        self._app = None

    def __call__(self, environ, start_response):
        if self._app is None:
            self._app = _load_application()
        return self._app(environ, start_response)


application = LazyApplication()
