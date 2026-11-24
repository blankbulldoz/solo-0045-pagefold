import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import Problem
from .images import register
from .sqlite_image import MAX_BYTES, mounted

ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class SourceConfigError(ValueError):
    """Raised when the operator-provided source policy cannot be loaded."""


class SourceConfig:
    def __init__(self, name, path, *, label=None, commit_sql=None,
                 commit_limit=1000, pages=16, timeout=10.0):
        if not ALIAS.fullmatch(name):
            raise SourceConfigError(f"非法采集源名称：{name!r}")
        if commit_sql is not None:
            if not isinstance(commit_sql, str) or not re.match(
                    r"\s*(?:SELECT|WITH)\b", commit_sql, re.IGNORECASE):
                raise SourceConfigError(f"采集源 {name} 的 commit_sql 必须是 SELECT/WITH 查询")
            if ";" in commit_sql.rstrip().rstrip(";"):
                raise SourceConfigError(f"采集源 {name} 的 commit_sql 不能包含多条语句")
        self.name = name
        self.path = Path(path).expanduser().resolve()
        self.label = label
        self.commit_sql = commit_sql
        try:
            self.commit_limit = max(1, min(int(commit_limit), MAX_BYTES))
            self.pages = max(1, min(int(pages), 1024))
            self.timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise SourceConfigError(f"采集源 {name} 的数值配置无效") from exc

    def public(self):
        return {
            "name": self.name,
            "label": self.label or self.name,
            "path": str(self.path),
            "available": self.path.is_file(),
            "commit_witness": bool(self.commit_sql),
            "pages_per_batch": self.pages,
        }


class SourcePolicy:
    """Server-side allow list. Requests never choose an arbitrary path directly."""

    def __init__(self, sources=(), roots=()):
        self.sources = {}
        for source in sources:
            self.add(source)
        self.roots = tuple(Path(root).expanduser().resolve() for root in roots)

    def add(self, source):
        if not isinstance(source, SourceConfig):
            raise SourceConfigError("sources 中的每一项都必须是 SourceConfig")
        if source.name in self.sources:
            raise SourceConfigError(f"采集源名称重复：{source.name}")
        self.sources[source.name] = source

    @property
    def enabled(self):
        return bool(self.sources or self.roots)

    @classmethod
    def from_config(cls, config_path=None, cli_sources=(), cli_roots=()):
        data = {}
        if config_path is not None:
            path = Path(config_path).expanduser().resolve()
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise SourceConfigError(f"无法读取采集源配置 {path}: {exc}") from exc
            if not isinstance(data, dict):
                raise SourceConfigError("采集源配置顶层必须是 JSON 对象")
        root_values = data.get("roots") or data.get("source_roots") or []
        policy = cls(roots=[*cli_roots, *root_values])
        configured = data.get("sources", data.get("allowed_sources", {}))
        if isinstance(configured, dict):
            entries = []
            for name, spec in configured.items():
                entry = {"name": name}
                if isinstance(spec, str):
                    entry["path"] = spec
                elif isinstance(spec, dict):
                    entry.update(spec)
                    entry["name"] = spec.get("name", name)
                else:
                    raise SourceConfigError(f"采集源 {name} 的配置无效")
                entries.append(entry)
        elif isinstance(configured, list):
            entries = configured
        else:
            raise SourceConfigError("sources 必须是对象或数组")
        for entry in entries:
            if not isinstance(entry, dict) or "name" not in entry or "path" not in entry:
                raise SourceConfigError("每个采集源须包含 name 和 path")
            policy.add(SourceConfig(**{key: entry[key] for key in entry
                                       if key in {"name", "path", "label", "commit_sql",
                                                  "commit_limit", "pages", "timeout"}}))
        for item in cli_sources:
            name, sep, raw_path = str(item).partition("=")
            if not sep:
                raw_path = name
                name = Path(raw_path).stem
            policy.add(SourceConfig(name, raw_path))
        return policy

    def describe(self):
        return {
            "enabled": self.enabled,
            "max_bytes": MAX_BYTES,
            "sources": [source.public() for source in self.sources.values()],
            "roots": [str(root) for root in self.roots],
        }

    def resolve(self, selector):
        if not self.enabled:
            raise Problem("collection_disabled", "服务端尚未配置允许采集的 SQLite 源。", 503)
        if not isinstance(selector, str) or not selector.strip():
            raise Problem("source_required", "请指定服务端配置中的采集源名称。", 400)
        name = selector.strip()
        if name in self.sources:
            return self.sources[name]

        candidate = Path(name).expanduser()
        candidates = []
        if candidate.is_absolute():
            candidates.append(candidate)
        else:
            candidates.extend(root / candidate for root in self.roots)
        for raw in candidates:
            resolved = raw.resolve()
            exact = next((item for item in self.sources.values() if item.path == resolved), None)
            if exact is not None:
                return exact
            if any(_inside(resolved, root) for root in self.roots):
                try:
                    return SourceConfig(resolved.stem, resolved)
                except SourceConfigError as exc:
                    raise Problem("source_forbidden", "源文件名不符合采集源命名规则。", 403) from exc
        raise Problem("source_forbidden", "该 SQLite 源不在服务端允许采集的清单内。", 403)


def _inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _json_native(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        import base64
        return {"type": "b", "base64": base64.b64encode(value).decode("ascii")}
    return str(value)


def read_commit_witness(db, sql, limit):
    """Read the transactional commit prefix contained in a fixed snapshot."""
    cursor = db.execute(sql)
    columns = [description[0] for description in cursor.description or []]
    rows = cursor.fetchmany(limit + 1)
    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]
    return {
        "columns": columns,
        "rows": [{columns[index]: _json_native(value) for index, value in enumerate(row)}
                 for row in rows],
        "count": len(rows),
        "truncated": truncated,
    }


def snapshot_facts(data, config):
    try:
        with mounted(data) as db:
            facts = {
                key: db.execute(f"PRAGMA {key}").fetchone()[0]
                for key in ("user_version", "schema_version", "application_id", "page_size")
            }
            commits = (read_commit_witness(db, config.commit_sql, config.commit_limit)
                       if config.commit_sql else None)
            return facts, commits
    except Problem:
        raise
    except sqlite3.Error as exc:
        raise Problem("source_commit_witness", f"无法读取时点提交凭证：{exc}", 502) from exc


def collect_source(store, policy, selector, *, label=None, observer=None):
    """Copy one source through SQLite's online backup API and register the result.

    The backup target is a single consistency point.  Commit metadata, catalog and
    row verification are all read from the serialized target, never from a mix of
    pre-backup and post-backup source connections.
    """
    config = policy.resolve(selector)
    if not config.path.is_file():
        raise Problem("source_unavailable", "配置的 SQLite 源当前不是可读取的普通文件。", 409)

    uri = config.path.as_uri() + "?mode=ro"
    captured_at = datetime.now(timezone.utc).isoformat()
    source = None
    target = None
    batches = 0
    total_pages = None
    page_size = None
    journal_mode = None
    try:
        source = sqlite3.connect(uri, uri=True, timeout=config.timeout, isolation_level=None)
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only=ON")
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
        journal_mode = source.execute("PRAGMA journal_mode").fetchone()[0]

        target = sqlite3.connect(":memory:", isolation_level=None)

        def progress(status, remaining, total):
            nonlocal batches, total_pages
            batches += 1
            total_pages = total
            if total and page_size and total * page_size > MAX_BYTES:
                raise Problem("image_size", "采集镜像超过 4 MiB 上限。", 413)
            if observer is not None:
                observer({
                    "batch": batches,
                    "status": status,
                    "remaining_pages": remaining,
                    "total_pages": total,
                    "pages_per_batch": config.pages,
                })

        try:
            source.backup(target, pages=config.pages, progress=progress, sleep=0.01)
        except Problem:
            raise
        except sqlite3.Error as exc:
            raise Problem("source_interrupted", f"一致读取源库时中断：{exc}", 502) from exc

        # A WAL source can preserve format-version bytes 2/2 in the backup header.
        # VACUUM operates only on the private target and emits the standalone 1/1
        # header accepted by Pagefold's image boundary.
        target.execute("VACUUM")
        data = target.serialize()
    except Problem:
        raise
    except sqlite3.Error as exc:
        raise Problem("source_read_failed", f"无法一致读取源库：{exc}", 502) from exc
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()

    if len(data) > MAX_BYTES:
        raise Problem("image_size", "采集镜像超过 4 MiB 上限。", 413)

    facts, commits = snapshot_facts(data, config)
    provenance = {
        "id": uuid.uuid4().hex,
        "source": config.name,
        "source_path": str(config.path),
        "captured_at": captured_at,
        "snapshot_digest": None,  # set immediately below and echoed by registration
        "sqlite_version": sqlite3.sqlite_version,
        "source_journal_mode": journal_mode,
        "page_size": page_size,
        "backup": {
            "batches": batches,
            "total_pages": total_pages,
            "pages_per_batch": config.pages,
            "backup_api": "sqlite3.Connection.backup",
        },
        "snapshot": facts,
        "included_commits": commits["rows"] if commits else [],
        "commit_columns": commits["columns"] if commits else [],
        "commit_count": commits["count"] if commits else 0,
        "commits_truncated": commits["truncated"] if commits else False,
        "commit_witness_configured": bool(config.commit_sql),
    }
    from .codec import digest
    provenance["snapshot_digest"] = digest(data)
    image_label = (label or config.label or f"采集自 {config.name}").strip()[:100]
    return register(store, data, image_label, collection=provenance)


def get_collection(db, collection_id):
    row = db.execute("SELECT record FROM source_collections WHERE id=?",
                     (collection_id,)).fetchone()
    if row is None:
        raise Problem("unknown_collection", "找不到指定的采集记录。", 404)
    return json.loads(row["record"] if isinstance(row["record"], str)
                      else row["record"].decode("utf-8"))


def list_collections(db):
    rows = db.execute("SELECT id,image_id,source,captured_at,record "
                      "FROM source_collections ORDER BY rowid DESC").fetchall()
    result = []
    for row in rows:
        record = json.loads(row["record"] if isinstance(row["record"], str)
                            else row["record"].decode("utf-8"))
        result.append({key: record.get(key) for key in (
            "id", "source", "captured_at", "snapshot_digest", "commit_count")} | {
                "image_id": row["image_id"]})
    return result
