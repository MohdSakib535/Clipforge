from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from app.config.settings import get_settings


class StorageService(ABC):
    """Abstract storage. Dev uses local disk; swap an S3 implementation in prod.

    Keys are POSIX-style relative paths, e.g. "videos/<id>/source.mp4".
    """

    @abstractmethod
    def path_for(self, key: str) -> Path:
        """Return a concrete local path for a key (materialising remote objects
        if needed). Callers pass this path to FFmpeg."""

    @abstractmethod
    def save_upload_chunk(self, key: str, offset: int, data: bytes) -> int:
        """Append/write a chunk at offset. Returns the new total size."""

    @abstractmethod
    def size(self, key: str) -> int: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...


class LocalStorageService(StorageService):
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or get_settings().storage_path)
        self.root.mkdir(parents=True, exist_ok=True)

    def _full(self, key: str) -> Path:
        # Guard against path traversal escaping the storage root.
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError(f"Illegal storage key: {key}")
        return p

    def path_for(self, key: str) -> Path:
        return self._full(key)

    def save_upload_chunk(self, key: str, offset: int, data: bytes) -> int:
        p = self._full(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Open in r+b if exists else create; write at offset for resumable uploads.
        mode = "r+b" if p.exists() else "wb"
        with open(p, mode) as fh:
            fh.seek(offset)
            fh.write(data)
        return p.stat().st_size

    def size(self, key: str) -> int:
        p = self._full(key)
        return p.stat().st_size if p.exists() else 0

    def exists(self, key: str) -> bool:
        return self._full(key).exists()

    def delete(self, key: str) -> None:
        p = self._full(key)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()


_storage: StorageService | None = None


def get_storage() -> StorageService:
    global _storage
    if _storage is None:
        _storage = LocalStorageService()
    return _storage
