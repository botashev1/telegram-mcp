"""Exercise Telethon's real media/stream path without making network requests."""
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl import functions, types
from telethon.utils import get_peer_id

from telegram_mcp.attachments import AttachmentError, AttachmentStore
from telegram_mcp.client import ScopedClient
from telegram_mcp.core import AllowList, ChatEntry

CHAT = -1000000000001


def sdk_client(monkeypatch, *, kind="document", size=8, chunks=(b"first", b"end")):
    inner = TelegramClient(StringSession(), 123, "0" * 32, receive_updates=False)
    inner.connect = AsyncMock(side_effect=AssertionError("Network connection must not be attempted"))
    if kind == "document":
        media = types.MessageMediaDocument(document=types.Document(
            id=1, access_hash=2, file_reference=b"synthetic", date=None,
            mime_type="application/pdf", size=size, dc_id=2,
            attributes=[types.DocumentAttributeFilename("note.pdf")],
        ))
    else:
        media = types.MessageMediaPhoto(photo=types.Photo(
            id=1, access_hash=2, file_reference=b"synthetic", date=None, dc_id=2,
            sizes=[types.PhotoSizeProgressive("x", 20, 20, [3, size])],
            video_sizes=[types.VideoSize("v", 20, 20, 100)],
        ))
    record = types.Message(id=10, peer_id=types.PeerChannel(1),
                           date=datetime(2026, 1, 1, tzinfo=timezone.utc), message="", media=media)
    inner.get_messages = AsyncMock(return_value=[record])
    requested = []

    async def download_chunks(location, **kwargs):
        requested.append((location, kwargs))
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr(inner, "_iter_download", download_chunks)
    policy = AllowList([ChatEntry("permitted", CHAT, "Permitted", "channel", 9)])
    return ScopedClient(inner, policy), requested


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["document", "photo"])
async def test_real_sdk_writes_complete_attachment(monkeypatch, tmp_path, kind):
    client, requested = sdk_client(monkeypatch, kind=kind)
    result = await AttachmentStore(tmp_path).download(
        lambda output, limit: client.download_attachment(CHAT, 10, output, limit)
    )
    assert Path(result["path"]).read_bytes() == b"firstend"
    assert result["bytes"] == 8
    assert result["sha256"] == sha256(b"firstend").hexdigest()
    assert len(requested) == 1
    location, download_options = requested[0]
    # No hidden SDK get_messages(ids=...) refresh may be enabled.
    assert download_options["msg_data"] is None
    if kind == "photo":
        assert isinstance(location, types.InputPhotoFileLocation)
        assert location.thumb_size == "x"
    else:
        assert isinstance(location, types.InputDocumentFileLocation)


@pytest.mark.asyncio
async def test_sdk_early_end_never_publishes_truncated_file(monkeypatch, tmp_path):
    client, _ = sdk_client(monkeypatch, chunks=(b"short",))
    with pytest.raises(AttachmentError):
        await AttachmentStore(tmp_path).download(
            lambda output, limit: client.download_attachment(CHAT, 10, output, limit)
        )
    assert not list(tmp_path.glob("attachment-*"))


@pytest.mark.asyncio
async def test_actual_sdk_bytes_cannot_exceed_limit(monkeypatch, tmp_path):
    client, _ = sdk_client(monkeypatch, size=4)
    with pytest.raises(AttachmentError, match="size limit"):
        await AttachmentStore(tmp_path, max_file_bytes=6).download(
            lambda output, limit: client.download_attachment(CHAT, 10, output, limit)
        )
    assert not list(tmp_path.glob("attachment-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("entry,peer", [
    (ChatEntry("permitted", -42, "Permitted", "chat"), types.PeerChat(42)),
    (ChatEntry("permitted", 42, "Permitted", "user", 9), types.PeerUser(42)),
    (ChatEntry("permitted", CHAT, "Permitted", "channel", 9), types.PeerChannel(1)),
])
async def test_real_sdk_message_lookup_is_peer_scoped_history(monkeypatch, tmp_path, entry, peer):
    inner = TelegramClient(StringSession(), 123, "0" * 32, receive_updates=False)
    media = types.MessageMediaDocument(document=types.Document(
        id=1, access_hash=2, file_reference=b"synthetic", date=None,
        mime_type="application/pdf", size=4, dc_id=2,
        attributes=[types.DocumentAttributeFilename("note.pdf")],
    ))
    record = types.Message(id=10, peer_id=peer,
                           date=datetime(2026, 1, 1, tzinfo=timezone.utc), message="", media=media)
    requests = []

    async def api_call(_client, request, *args, **kwargs):
        requests.append(request)
        assert isinstance(request, functions.messages.GetHistoryRequest)
        assert get_peer_id(request.peer) == entry.id
        assert request.limit == 1
        assert request.offset_id == 11
        return types.messages.Messages(messages=[record], topics=[], chats=[], users=[])

    async def download_chunks(location, **kwargs):
        assert kwargs["msg_data"] is None
        yield b"data"

    monkeypatch.setattr(TelegramClient, "__call__", api_call)
    monkeypatch.setattr(inner, "_iter_download", download_chunks)
    client = ScopedClient(inner, AllowList([entry]))
    output = BytesIO()
    await client.download_attachment(entry.id, 10, output, 100)
    assert output.getvalue() == b"data"
    assert len(requests) == 1
