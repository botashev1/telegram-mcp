import asyncio
import errno
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_mcp.attachments import AttachmentError, AttachmentStore
from telegram_mcp.core import NotConfigured


def cached_files(path):
    return [p for p in path.iterdir() if p.name != ".download.lock"]


async def sample(output, limit):
    output.write(b"example")
    return {"file_name": "../../private/report.PDF", "mime_type": "application/pdf", "size": 7}


@pytest.mark.asyncio
async def test_download_generated_path_digest_and_private_permissions(tmp_path):
    result = await AttachmentStore(tmp_path).download(sample)
    path = Path(result["path"])
    assert path.parent == tmp_path
    assert path.name.startswith("attachment-") and path.suffix == ".pdf"
    assert path.read_bytes() == b"example"
    assert path.stat().st_mode & 0o777 == 0o600
    assert result["bytes"] == 7
    assert result["sha256"] == hashlib.sha256(b"example").hexdigest()
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.asyncio
async def test_repeated_download_does_not_overwrite(tmp_path):
    store = AttachmentStore(tmp_path)
    first = await store.download(sample)
    second = await store.download(sample)
    assert first["path"] != second["path"]
    assert Path(first["path"]).read_bytes() == Path(second["path"]).read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("file_limit,cache_limit", [(6, 100), (100, 6)])
async def test_actual_bytes_bounded_and_partial_removed(tmp_path, file_limit, cache_limit):
    async def too_large(output, limit):
        output.write(b"123")
        output.write(b"4567")
        return {"file_name": "data.bin", "size": 1}
    with pytest.raises(AttachmentError, match="size limit"):
        await AttachmentStore(tmp_path, max_file_bytes=file_limit,
                              max_cache_bytes=cache_limit).download(too_large)
    assert cached_files(tmp_path) == []


@pytest.mark.asyncio
async def test_total_quota_counts_existing_files_before_fetch(tmp_path):
    (tmp_path / "existing.pdf").write_bytes(b"123456")
    calls = []
    async def record_limit(output, limit):
        calls.append(limit)
        return await sample(output, limit)
    with pytest.raises(AttachmentError, match="size limit"):
        await AttachmentStore(tmp_path, max_cache_bytes=10).download(record_limit)
    assert calls == [4]
    assert [p.name for p in cached_files(tmp_path)] == ["existing.pdf"]


@pytest.mark.asyncio
async def test_file_count_quota_prevents_fetch(tmp_path):
    (tmp_path / "existing.pdf").write_bytes(b"x")
    async def forbidden(*args):
        pytest.fail("No network call when the cache is full")
    with pytest.raises(AttachmentError, match="cache is full"):
        await AttachmentStore(tmp_path, max_files=1).download(forbidden)


@pytest.mark.asyncio
async def test_error_is_sanitized_and_partial_removed(tmp_path):
    async def failing(output, limit):
        output.write(b"partial")
        raise RuntimeError("synthetic-secret-must-not-escape")
    with pytest.raises(AttachmentError) as error:
        await AttachmentStore(tmp_path).download(failing)
    assert str(error.value) == "Attachment could not be downloaded."
    assert cached_files(tmp_path) == []


@pytest.mark.asyncio
async def test_timeout_cleans_partial_file(tmp_path):
    async def hanging(output, limit):
        output.write(b"partial")
        await asyncio.Event().wait()
    with pytest.raises(AttachmentError, match="timed out"):
        await AttachmentStore(tmp_path, timeout=0.01).download(hanging)
    assert cached_files(tmp_path) == []


@pytest.mark.asyncio
async def test_cancellation_cleans_and_unlocks_cache(tmp_path):
    started = asyncio.Event()
    async def hanging(output, limit):
        output.write(b"partial")
        started.set()
        await asyncio.Event().wait()
    store = AttachmentStore(tmp_path)
    task = asyncio.create_task(store.download(hanging))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cached_files(tmp_path) == []
    await store.download(sample)


@pytest.mark.asyncio
async def test_concurrent_download_refused_without_blocking_event_loop(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    async def waiting(output, limit):
        started.set()
        await release.wait()
        return await sample(output, limit)
    first = asyncio.create_task(AttachmentStore(tmp_path).download(waiting))
    try:
        await started.wait()
        with pytest.raises(AttachmentError, match="in progress"):
            await AttachmentStore(tmp_path).download(sample)
    finally:
        release.set()
        await first


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["attachment-other.pdf", ".download.lock"])
async def test_symlink_cannot_redirect_writes(tmp_path, entry):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_bytes(b"untouched")
    (tmp_path / entry).symlink_to(outside)
    with pytest.raises(AttachmentError):
        await AttachmentStore(tmp_path).download(sample)
    assert outside.read_bytes() == b"untouched"


@pytest.mark.asyncio
async def test_hard_link_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    os.link(target, tmp_path / "alias")
    with pytest.raises(AttachmentError, match="unexpected entry"):
        await AttachmentStore(tmp_path).download(sample)


@pytest.mark.asyncio
async def test_writable_by_others_cache_is_rejected(tmp_path):
    tmp_path.chmod(0o777)
    with pytest.raises(AttachmentError, match="not writable by others"):
        await AttachmentStore(tmp_path).download(sample)


@pytest.mark.asyncio
async def test_symlink_cache_directory_is_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(AttachmentError, match="canonical"):
        await AttachmentStore(alias).download(sample)


@pytest.mark.asyncio
async def test_empty_download_is_not_published(tmp_path):
    async def empty(output, limit):
        return {"file_name": "empty.pdf"}
    with pytest.raises(AttachmentError, match="empty attachment"):
        await AttachmentStore(tmp_path).download(empty)
    assert cached_files(tmp_path) == []


@pytest.mark.asyncio
async def test_incomplete_download_is_not_published(tmp_path):
    async def truncated(output, limit):
        output.write(b"partial")
        return {"file_name": "report.pdf", "size": 100}
    with pytest.raises(AttachmentError, match="does not match"):
        await AttachmentStore(tmp_path).download(truncated)
    assert cached_files(tmp_path) == []


def test_download_disabled_without_owner_config(monkeypatch):
    monkeypatch.delenv("TG_DOWNLOAD_DIR", raising=False)
    with pytest.raises(NotConfigured, match="not configured"):
        AttachmentStore.from_environment()


@pytest.mark.asyncio
async def test_failed_creation_releases_lock_and_retry_succeeds(tmp_path, monkeypatch):
    original = os.open
    def fail_partial(path, *args, **kwargs):
        if str(path).endswith(".part"):
            raise OSError(errno.ENOSPC, "Synthetic full disk")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as context:
        context.setattr(os, "open", fail_partial)
        with pytest.raises(AttachmentError, match="safely"):
            await AttachmentStore(tmp_path).download(sample)
    await AttachmentStore(tmp_path).download(sample)


@pytest.mark.asyncio
async def test_name_collision_never_removes_existing_partial(tmp_path, monkeypatch):
    monkeypatch.setattr("telegram_mcp.attachments.uuid.uuid4", lambda: SimpleNamespace(hex="collision"))
    existing = tmp_path / "attachment-collision.part"
    existing.write_bytes(b"preserve")
    with pytest.raises(AttachmentError, match="safely"):
        await AttachmentStore(tmp_path).download(sample)
    assert existing.read_bytes() == b"preserve"


@pytest.mark.asyncio
async def test_cleanup_failure_still_releases_lock(tmp_path, monkeypatch):
    async def broken(output, limit):
        output.write(b"partial")
        raise RuntimeError("synthetic failure")
    with monkeypatch.context() as context:
        def fail_unlink(*args, **kwargs):
            raise PermissionError("synthetic filesystem failure")
        context.setattr(os, "unlink", fail_unlink)
        with pytest.raises(AttachmentError, match="could not be downloaded"):
            await AttachmentStore(tmp_path).download(broken)
    assert len(list(tmp_path.glob("*.part"))) == 1
    await AttachmentStore(tmp_path).download(sample)
