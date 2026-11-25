import json
import logging
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import api, workspace
from .errors import Problem
from .inputs import payload
from .lifecycle import Lifecycle


class Handler(BaseHTTPRequestHandler):
    server_version = "Pagefold/1"

    def setup(self):
        self.request.settimeout(15)
        super().setup()

    def send_bytes(self, data, content_type, status=200, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def json(self, result, status=200, headers=()):
        self.send_bytes(json.dumps(result, ensure_ascii=True).encode("ascii"),
                        "application/json; charset=utf-8", status, headers)

    def redirect(self, path):
        self.send_bytes(b"", "text/plain", 303, [("Location", path)])

    def report(self, problem, path):
        if path.startswith("/api/"):
            self.json(problem.as_dict(), problem.status, problem.headers)
        else:
            back = "/".join(path.split("/")[:3])
            self.send_bytes(workspace.error(problem, back), "text/html; charset=utf-8",
                            problem.status, problem.headers)

    def lifecycle_endpoint(self, path):
        status = self.server.lifecycle.status()
        if path == "/healthz":
            self.json({"ok": True, **status})
            return
        if path == "/-/status":
            self.json(status)
            return
        code = 200 if status["mode"] == "running" else 503
        self.json(status, code, [("Retry-After", "30")] if code == 503 else ())

    def route_get(self, store, path, parts, query):
        if path.startswith("/assets/"):
            body, kind = workspace.asset(path)
            self.send_bytes(body, kind + "; charset=utf-8")
        elif len(parts) > 1 and parts[0] == "api":
            result = api.read(store, parts[1:], query)
            if isinstance(result, bytes):
                self.send_bytes(result, "application/vnd.sqlite3", headers=[
                    ("Content-Disposition", f'attachment; filename="pagefold-{parts[2]}.sqlite"')])
            else:
                headers = []
                if len(parts) == 4 and parts[3] == "certificate":
                    headers.append(("Content-Disposition", f'attachment; filename="pagefold-{parts[2]}.json"'))
                self.json(result, headers=headers)
        else:
            self.send_bytes(workspace.workspace(store, parts, query), "text/html; charset=utf-8")

    def route_post(self, store, path, parts):
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.origins:
            raise Problem("origin", "该请求必须来自当前工作区。", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise Problem("origin", "该请求必须来自当前工作区。", 403)
        if path == "/api/images":
            self.json(api.upload(store, self), 201)
        elif path == "/demo":
            payload(self, 0)
            self.redirect(f"/plans/{workspace.sample(store)['id']}")
        elif len(parts) >= 2 and parts[:2] == ["api", "plans"]:
            self.json(api.write_plan(store, self, parts[2:]), 201 if len(parts) == 2 else 200)
        elif parts and parts[0] == "plans":
            result = api.write_plan(store, self, parts[1:])
            self.redirect(f"/plans/{parts[1] if len(parts) > 1 else result['id']}")
        else:
            raise Problem("route", "找不到该入口。", 404)

    def handle_route(self, write):
        path = self.path
        admitted = False
        download = False
        try:
            if self.headers.get("Host") not in self.server.hosts:
                raise Problem("host", "请通过本机工作区的地址访问。", 403)
            target = urlsplit(path)
            path = target.path
            parts = [part for part in path.split("/") if part]
            if path in {"/healthz", "/readyz", "/-/status"}:
                self.lifecycle_endpoint(path)
                return
            download = (not write and len(parts) == 4 and parts[:2] == ["api", "plans"]
                        and parts[3] == "download")
            self.server.lifecycle.admit(write=write, download=download)
            admitted = True
            if write:
                self.route_post(self.server.store, path, parts)
            else:
                query = {key: values[-1] for key, values in parse_qs(
                    target.query, keep_blank_values=True, max_num_fields=16).items()}
                self.route_get(self.server.store, path, parts, query)
        except Problem as exc:
            self.report(exc, path)
        except ValueError:
            self.report(Problem("request", "请求参数无法读取。", 400), path)
        except (BrokenPipeError, ConnectionResetError):
            return
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY:
                self.report(Problem("state_busy", "状态库繁忙，请稍后重试。", 503), path)
            else:
                logging.exception("Request failed")
                self.report(Problem("internal", "服务暂时无法完成请求。", 500), path)
        except Exception:
            logging.exception("Request failed")
            self.report(Problem("internal", "服务暂时无法完成请求。", 500), path)
        finally:
            if admitted:
                self.server.lifecycle.finish(write=write, download=download)

    def do_GET(self):
        self.handle_route(False)

    def do_POST(self):
        self.handle_route(True)


def make_server(store, port, drain_timeout=30.0):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.store = store
    server.lifecycle = Lifecycle(drain_timeout)
    address = server.server_port
    server.hosts = {f"127.0.0.1:{address}", f"localhost:{address}"}
    server.origins = {f"http://{host}" for host in server.hosts}
    return server
