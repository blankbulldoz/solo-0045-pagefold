import json
import logging
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import api, workspace
from .errors import Problem
from .inputs import payload
from .releases import Download


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

    def security_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")

    def download_headers(self, download):
        self.send_header("ETag", download.etag)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f'attachment; filename="{download.filename}"')
        self.security_headers()

    def send_download(self, download):
        # 续领前先用凭证摘要派生的强校验器确认手中部分文件与候选是同一份。
        tags = self.etag_list(self.headers.get_all("If-Match"))
        if tags is not None and "*" not in tags and download.etag not in tags:
            return self.send_bytes(b"", "text/plain; charset=utf-8", 412, [
                ("ETag", download.etag), ("Accept-Ranges", "bytes")])
        matched = self.etag_list(self.headers.get_all("If-None-Match"))
        if matched is not None and ("*" in matched or download.etag in matched):
            self.send_response(304)
            self.send_header("ETag", download.etag)
            self.send_header("Accept-Ranges", "bytes")
            self.security_headers()
            self.end_headers()
            return
        # If-Range 不匹配（代理换成了另一份候选）时，整份重置而不是拼接错误字节。
        range_header = self.headers.get("Range")
        if range_header is not None:
            validator = self.headers.get("If-Range")
            if validator is not None and validator != download.etag:
                range_header = None
        span = self.byte_range(range_header, download.size) if range_header is not None else None
        if range_header is not None and span is None:
            return self.send_bytes(
                f"该范围无法满足，文件共 {download.size} 字节。".encode("utf-8"),
                "text/plain; charset=utf-8", 416,
                [("Content-Range", f"bytes */{download.size}"), ("ETag", download.etag),
                 ("Accept-Ranges", "bytes")])
        if span is None:
            return self.send_bytes(download.body, "application/vnd.sqlite3", headers=[
                ("ETag", download.etag), ("Accept-Ranges", "bytes"),
                ("Content-Disposition", f'attachment; filename="{download.filename}"')])
        start, end = span
        self.send_response(206)
        self.send_header("Content-Type", "application/vnd.sqlite3")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Range", f"bytes {start}-{end}/{download.size}")
        self.download_headers(download)
        self.end_headers()
        self.wfile.write(download.body[start:end + 1])

    @staticmethod
    def etag_list(values):
        if not values:
            return None
        tags = set()
        for value in values:
            if "*" in value:
                tags.add("*")
            for index, char in enumerate(value):
                if char == '"':
                    closing = value.find('"', index + 1)
                    if closing == -1:
                        continue
                    tags.add(value[index:closing + 1])
        return tags

    @staticmethod
    def byte_range(header, size):
        # 仅支持单一字节区间：bytes=start-end、bytes=start-、bytes=-suffix。
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip().lower())
        if not match or size == 0:
            return None
        first, last = match.groups()
        if first == "":
            if last == "":
                return None
            suffix = int(last)
            if suffix == 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
            if start >= size or end < start:
                return None
            end = min(end, size - 1)
        return start, end

    def report(self, problem, path):
        if path.startswith("/api/"):
            self.json(problem.as_dict(), problem.status)
        else:
            back = "/".join(path.split("/")[:3])
            self.send_bytes(workspace.error(problem, back), "text/html; charset=utf-8", problem.status)

    def route_get(self, store, path, parts, query):
        if path.startswith("/assets/"):
            body, kind = workspace.asset(path)
            self.send_bytes(body, kind + "; charset=utf-8")
        elif len(parts) > 1 and parts[0] == "api":
            result = api.read(store, parts[1:], query)
            if isinstance(result, Download):
                self.send_download(result)
            elif isinstance(result, bytes):
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
        try:
            if self.headers.get("Host") not in self.server.hosts:
                raise Problem("host", "请通过本机工作区的地址访问。", 403)
            target = urlsplit(path)
            path = target.path
            parts = [part for part in path.split("/") if part]
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

    def do_GET(self):
        self.handle_route(False)

    def do_POST(self):
        self.handle_route(True)


def make_server(store, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.store = store
    address = server.server_port
    server.hosts = {f"127.0.0.1:{address}", f"localhost:{address}"}
    server.origins = {f"http://{host}" for host in server.hosts}
    return server
