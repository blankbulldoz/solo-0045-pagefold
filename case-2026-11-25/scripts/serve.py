#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""以隔离模式启动持久化的 Pagefold 服务，状态库放在案卷材料目录内。"""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from pagefold.server import make_server  # noqa: E402
from pagefold.store import Store  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8745
STATE = Path(__file__).resolve().parent.parent / "materials" / "state" / "pagefold-state.sqlite"

with make_server(Store(STATE), PORT) as server:
    print(f"Pagefold serving on http://127.0.0.1:{server.server_port} state={STATE}", flush=True)
    server.serve_forever()
