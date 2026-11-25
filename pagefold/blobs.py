import hashlib
import os
import re
import tempfile
from pathlib import Path

from .errors import Problem

DIGEST = re.compile(r"[0-9a-f]{64}")


def blob_path(state_path):
    """状态库对应的内容库目录，与状态库文件并列。"""
    return Path(str(state_path) + ".blobs")


class BlobStore:
    """按内容寻址的不可变文件库。

    文件以原始字节的 SHA-256 命名，同一份内容无论被多少来源或成果
    引用都只保存一次。写入先落临时文件、fsync 后原子换名，因此中断
    只会留下可重写的临时文件，正式文件要么完整要么不存在。
    """

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _final(self, digest):
        if not DIGEST.fullmatch(digest or ""):
            raise Problem("blob_digest", "内容摘要格式无效。", 500)
        return self.root / digest

    def has(self, digest):
        return self._final(digest).is_file()

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        final = self.root / digest
        if final.exists():
            if final.stat().st_size != len(data):
                raise Problem("blob_conflict", "内容库中存在同名但长度不符的文件。", 500)
            return digest
        fd, temp = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, final)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        return digest

    def get(self, digest):
        final = self._final(digest)
        try:
            data = final.read_bytes()
        except FileNotFoundError:
            raise Problem("blob_missing", "内容库缺少状态库引用的文件。", 500) from None
        if hashlib.sha256(data).hexdigest() != digest:
            raise Problem("blob_corrupt", "内容库文件与摘要不一致。", 500)
        return data

    def files(self):
        return sorted(path.name for path in self.root.iterdir()
                      if path.is_file() and DIGEST.fullmatch(path.name))

    def stray(self):
        return sorted(path.name for path in self.root.iterdir()
                      if not DIGEST.fullmatch(path.name))
