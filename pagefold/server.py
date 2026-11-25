"""基于标准库 wsgiref 的 WSGI 运行器。

旧实现使用 ``ThreadingHTTPServer`` + ``BaseHTTPRequestHandler`` 承载自定义协议；
迁移后协议处理全部位于 :mod:`pagefold.wsgi`，这里只负责把标准 WSGI 应用挂到
标准库的多线程 WSGI 服务器上，供本机演练和临时服务使用。生产环境可改用
gunicorn、uWSGI 等外部 WSGI 服务器直接引用 :data:`pagefold.wsgi.application`。
"""

from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

from .wsgi import make_app


class ThreadingWSGIServer(WSGIServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        socket, address = super().get_request()
        socket.settimeout(15)
        return socket, address


class QuietHandler(WSGIRequestHandler):
    server_version = "PagefoldWSGI/1"

    def log_message(self, *args):
        pass


def make_server(store, port, allowed_hosts=None):
    """返回在 127.0.0.1:port 监听的标准 WSGI 服务器；port=0 时由内核分配端口。"""
    server = ThreadingWSGIServer(("127.0.0.1", port), QuietHandler)
    server.store = store
    if allowed_hosts is None:
        address = server.server_port
        allowed_hosts = [f"127.0.0.1:{address}", f"localhost:{address}"]
    server.allowed_hosts = list(allowed_hosts)
    application = make_app(store, allowed_hosts=server.allowed_hosts)
    server.application = application
    server.set_app(application)
    return server
