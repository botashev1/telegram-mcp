"""Bounded downloads into an owner-configured, dedicated local cache."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import stat
import uuid
from contextlib import suppress
from pathlib import Path

from .core import ChatNotAllowed, NotConfigured

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_CACHE_BYTES = 250 * 1024 * 1024
MAX_CACHE_FILES = 100
DOWNLOAD_TIMEOUT = 120


class AttachmentError(ValueError):
    """Safe, fixed error messages suitable for returning through MCP."""


class _BoundedWriter:
    def __init__(self, stream, limit: int):
        self._stream = stream
        self._limit = limit
        self.size = 0
        self.digest = hashlib.sha256()

    def write(self, data):
        if self.size + len(data) > self._limit:
            raise AttachmentError("Attachment exceeds the download or cache size limit.")
        written = self._stream.write(data)
        self.digest.update(data[:written])
        self.size += written
        return written

    def flush(self):
        self._stream.flush()

    def tell(self):
        return self.size


def _suffix(metadata: dict) -> str:
    suffix = Path(metadata.get("file_name") or "").suffix
    if re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix):
        return suffix.lower()
    return {"image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf"}.get(
        metadata.get("mime_type"), ".bin"
    )


class AttachmentStore:
    def __init__(self, directory, *, max_file_bytes=MAX_FILE_BYTES,
                 max_cache_bytes=MAX_CACHE_BYTES, max_files=MAX_CACHE_FILES,
                 timeout=DOWNLOAD_TIMEOUT):
        self.directory = Path(directory)
        self.max_file_bytes = max_file_bytes
        self.max_cache_bytes = max_cache_bytes
        self.max_files = max_files
        self.timeout = timeout

    @classmethod
    def from_environment(cls):
        directory = os.environ.get("TG_DOWNLOAD_DIR")
        if not directory:
            raise NotConfigured("Attachment downloads are not configured by the owner.")
        return cls(directory)

    def _open_directory(self):
        try:
            if not self.directory.is_absolute() or self.directory.resolve(strict=True) != self.directory:
                raise AttachmentError("Download directory must be an existing canonical absolute path.")
            descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise AttachmentError("The configured download directory is unavailable.") from None
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            os.close(descriptor)
            raise AttachmentError("Download directory must be service-owned and not writable by others.")
        return descriptor

    def _usage(self, directory_fd):
        total = count = 0
        for name in os.listdir(directory_fd):
            if name == ".download.lock":
                continue
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise AttachmentError(
                    "Download cache contains an unexpected entry; owner cleanup is required."
                )
            total += info.st_size
            count += 1
        if count >= self.max_files or total >= self.max_cache_bytes:
            raise AttachmentError("Download cache is full; owner cleanup is required.")
        return min(self.max_file_bytes, self.max_cache_bytes - total)

    async def download(self, fetch):
        """fetch(output, byte_limit) returns metadata; never accepts an output path."""
        directory_fd = self._open_directory()
        lock_fd = None
        temporary = None
        try:
            lock_fd = os.open(".download.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                              0o600, dir_fd=directory_fd)
            lock_info = os.fstat(lock_fd)
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                    or lock_info.st_uid != os.geteuid()):
                raise AttachmentError("Download cache lock is invalid.")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AttachmentError("Another attachment download is in progress; retry later.") from None
            limit = self._usage(directory_fd)
            identifier = "attachment-" + uuid.uuid4().hex
            candidate = identifier + ".part"
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory_fd)
            temporary = candidate
            with os.fdopen(descriptor, "wb") as stream:
                output = _BoundedWriter(stream, limit)
                try:
                    metadata = await asyncio.wait_for(fetch(output, limit), timeout=self.timeout)
                except (AttachmentError, ChatNotAllowed):
                    raise
                except asyncio.TimeoutError:
                    raise AttachmentError("Attachment download timed out.") from None
                except Exception:
                    # SDK exceptions can contain internal paths or account metadata.
                    raise AttachmentError("Attachment could not be downloaded.") from None
                if output.size == 0:
                    raise AttachmentError("Telegram returned an empty attachment.")
                if metadata.get("size") != output.size:
                    raise AttachmentError("Attachment size does not match Telegram metadata.")
                stream.flush()
                os.fsync(stream.fileno())
            final_name = identifier + _suffix(metadata)
            # link() fails rather than overwriting, even on a name collision.
            os.link(temporary, final_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    follow_symlinks=False)
            os.unlink(temporary, dir_fd=directory_fd)
            temporary = None
            return {"path": str(self.directory / final_name), "bytes": output.size,
                    "sha256": output.digest.hexdigest(), "attachment": metadata}
        except OSError:
            raise AttachmentError("Attachment cache could not be accessed safely.") from None
        finally:
            if temporary is not None:
                # A filesystem error must not retain the lock or replace the
                # original failure. A leftover partial still counts toward quota.
                with suppress(OSError):
                    os.unlink(temporary, dir_fd=directory_fd)
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(directory_fd)
