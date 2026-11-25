#!/usr/bin/env bash
# Install or upgrade Pagefold on a current Linux host using systemd.
#
# Run from a clean checkout:
#   sudo deploy/install_systemd.sh
#
# The script stops new writes on an active service as its first mutation,
# waits for admitted writes to finish, stages a timestamped release under
# /opt/pagefold/releases, atomically swaps /opt/pagefold/current, then lets
# systemd perform the SIGTERM drain/restart.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASES_DIR="/opt/pagefold/releases"
CURRENT_LINK="/opt/pagefold/current"
STATE_DIR="/var/lib/pagefold"
SERVICE_FILE="/etc/systemd/system/pagefold.service"
PORT="${PAGEFOLD_PORT:-8745}"
WRITE_DRAIN_TIMEOUT="${PAGEFOLD_WRITE_DRAIN_TIMEOUT:-60}"

if [[ ${EUID} -ne 0 ]]; then
  echo "请用 root 或 sudo 运行：sudo $0" >&2
  exit 2
fi

if ! python3 - <<'PY'
import sqlite3
import sys
sys.exit(0 if sqlite3.sqlite_version_info >= (3, 37) and
         hasattr(sqlite3.Connection, "deserialize") and
         hasattr(sqlite3.Connection, "serialize") else 1)
PY
then
  echo "Python 3/SQLite 运行时不满足 SQLite 3.37+ 且需要 serialize/deserialize。" >&2
  exit 2
fi

if systemctl is-active --quiet pagefold.service; then
  echo "→ 尽早停止接收新裁决/重演/封存（systemctl reload = SIGUSR1）"
  systemctl reload pagefold.service || true
  PAGEFOLD_PORT="$PORT" PAGEFOLD_WRITE_DRAIN_TIMEOUT="$WRITE_DRAIN_TIMEOUT" python3 - <<'PY'
import json
import os
import sys
import time
import urllib.request

port = os.environ["PAGEFOLD_PORT"]
deadline = time.monotonic() + float(os.environ["PAGEFOLD_WRITE_DRAIN_TIMEOUT"])
last = None
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/-/status", timeout=2) as response:
            last = json.loads(response.read())
        if last.get("active_writes", 1) == 0:
            print(f"  accepted writes drained: {last}")
            sys.exit(0)
    except Exception:
        pass
    time.sleep(0.2)
raise SystemExit(f"等待 active_writes=0 超时：{last}")
PY
fi

id pagefold >/dev/null 2>&1 || useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin pagefold
mkdir -p "$RELEASES_DIR" "$STATE_DIR"
chown -R pagefold:pagefold "$STATE_DIR"

release_dir="$RELEASES_DIR/$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$release_dir"
echo "→ 暂存新版本到 $release_dir"
cp -a "$SOURCE_ROOT/pagefold" "$SOURCE_ROOT/examples" "$SOURCE_ROOT/deploy" \
      "$SOURCE_ROOT/README.md" "$SOURCE_ROOT/PROJECT_SPEC.md" "$release_dir/"
python3 -m compileall -q "$release_dir/pagefold" "$release_dir/examples"

ln -sfn "$release_dir" "$CURRENT_LINK.new"
mv -Tf "$CURRENT_LINK.new" "$CURRENT_LINK"
install -m 0644 "$CURRENT_LINK/deploy/systemd/pagefold.service" "$SERVICE_FILE"

echo "→ daemon-reload 并重启；SIGTERM 会等待已接受请求和下载"
systemctl daemon-reload
systemctl enable pagefold.service >/dev/null 2>&1 || true
systemctl restart pagefold.service

PAGEFOLD_PORT="$PORT" python3 - <<'PY'
import json
import os
import time
import urllib.request
port = os.environ["PAGEFOLD_PORT"]
deadline = time.monotonic() + 30
last_error = None
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            body = json.loads(response.read())
        if response.status == 200:
            print(f"  Pagefold healthy: {body}")
            raise SystemExit(0)
    except Exception as exc:
        last_error = exc
        time.sleep(0.2)
raise SystemExit(f"服务重启后未通过 /healthz：{last_error}")
PY

echo "完成：http://127.0.0.1:${PORT}；状态库 ${STATE_DIR}/state.sqlite"
