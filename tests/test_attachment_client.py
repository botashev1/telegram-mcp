from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types
from telethon.utils import get_peer_id

from telegram_mcp.client import ScopedClient
from telegram_mcp.core import AllowList, ChatEntry, ChatNotAllowed
from telegram_mcp.media import attachment_metadata

CHAT = -1000000000001
OTHER_CHAT = -1000000000002


def document(size=4, file_name="notes.pdf"):
    return types.MessageMediaDocument(document=types.Document(
        id=17, access_hash=23, file_reference=b"private-reference", date=None,
        mime_type="application/pdf", size=size, dc_id=2,
        attributes=[types.DocumentAttributeFilename(file_name)],
    ))


def photo():
    return types.MessageMediaPhoto(photo=types.Photo(
        id=19, access_hash=29, file_reference=b"private-reference", date=None, dc_id=2,
        sizes=[types.PhotoSize("s", 10, 10, 2), types.PhotoSizeProgressive("x", 20, 20, [3, 4])],
        video_sizes=[types.VideoSize("v", 20, 20, 100)],
    ))


def message(media=None, *, message_id=10, channel_id=1, noforwards=None):
    return types.Message(
        id=message_id, peer_id=types.PeerChannel(channel_id),
        date=datetime(2026, 1, 1, tzinfo=timezone.utc), message="caption",
        media=media, noforwards=noforwards,
    )


def scoped(records):
    async def download(record, *, file, **kwargs):
        file.write(b"data")
        return file
    inner = SimpleNamespace(get_messages=AsyncMock(return_value=records),
                            download_media=AsyncMock(side_effect=download))
    policy = AllowList([ChatEntry("permitted", CHAT, "Permitted", "channel", 123)])
    return ScopedClient(inner, policy), inner


@pytest.mark.parametrize("media,kind", [(document(), "document"), (photo(), "photo")])
def test_metadata_uses_only_safe_message_fields(media, kind):
    result = attachment_metadata(message(media))
    assert result["kind"] == kind
    assert result["size"] == 4
    assert result["downloadable"] is True
    assert set(result) == {"kind", "file_name", "mime_type", "size", "downloadable"}
    assert "private-reference" not in str(result)


def test_filename_is_sanitized_display_label():
    result = attachment_metadata(message(document(file_name="..\\..\\bad\u202ename\x00.pdf")))
    assert result["file_name"] == "badname.pdf"


@pytest.mark.parametrize("media", [None, types.MessageMediaContact(
    phone_number="123", first_name="A", last_name="B", vcard="hidden", user_id=1,
), types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))])
def test_non_attachment_media_ignored(media):
    assert attachment_metadata(message(media)) is None


@pytest.mark.parametrize("media", [document(), photo()])
def test_disappearing_media_not_downloadable(media):
    media.ttl_seconds = 10
    assert attachment_metadata(message(media))["downloadable"] is False


def test_protected_message_not_downloadable():
    assert attachment_metadata(message(document(), noforwards=True))["downloadable"] is False


@pytest.mark.asyncio
async def test_forbidden_chat_denied_before_any_api_call():
    client, inner = scoped([message(document())])
    with pytest.raises(ChatNotAllowed):
        await client.download_attachment(OTHER_CHAT, 10, BytesIO(), 100)
    inner.get_messages.assert_not_awaited()
    inner.download_media.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", [True, 0, -1, "10", 1.5])
async def test_invalid_message_id_denied_before_api(message_id):
    client, inner = scoped([message(document())])
    with pytest.raises(ValueError, match="positive integer"):
        await client.download_attachment(CHAT, message_id, BytesIO(), 100)
    inner.get_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_output_path_not_accepted():
    client, inner = scoped([message(document())])
    with pytest.raises(ValueError, match="output stream"):
        await client.download_attachment(CHAT, 10, "/tmp/arbitrary", 100)
    inner.get_messages.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("record", [message(document(), channel_id=2),
                                    message(document(), message_id=11)])
async def test_returned_chat_and_message_identity_checked(record):
    client, inner = scoped([record])
    with pytest.raises(ChatNotAllowed, match="identity"):
        await client.download_attachment(CHAT, 10, BytesIO(), 100)
    inner.download_media.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("records", [None, [], [None], [message(document()), message(document())]])
async def test_missing_or_ambiguous_result_not_downloaded(records):
    client, inner = scoped(records)
    with pytest.raises(ValueError, match="unavailable"):
        await client.download_attachment(CHAT, 10, BytesIO(), 100)
    inner.download_media.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("record", [
    message(), message(document(size=0)), message(document(), noforwards=True),
    message(types.MessageMediaDocument(ttl_seconds=3, document=document().document)),
])
async def test_unsupported_and_protected_attachments_not_downloaded(record):
    client, inner = scoped([record])
    with pytest.raises(ValueError, match="supported downloadable"):
        await client.download_attachment(CHAT, 10, BytesIO(), 100)
    inner.download_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_advertised_oversize_refused_before_download():
    client, inner = scoped([message(document(size=101))])
    with pytest.raises(ValueError, match="size limit"):
        await client.download_attachment(CHAT, 10, BytesIO(), 100)
    inner.download_media.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("media", [document(), photo()])
async def test_download_uses_exact_peer_message_and_stream(media):
    record = message(media)
    client, inner = scoped([record])
    output = BytesIO()
    result = await client.download_attachment(CHAT, 10, output, 100)
    assert output.getvalue() == b"data"
    args, kwargs = inner.get_messages.call_args
    assert get_peer_id(args[0]) == CHAT
    assert args[0].access_hash == 123
    assert kwargs == {"limit": 1, "min_id": 9, "max_id": 11}
    args, kwargs = inner.download_media.call_args
    assert args == (record.media,)
    assert kwargs["file"] is output
    if result["kind"] == "photo":
        assert kwargs["thumb"] == "x"  # Static photo, not the larger video.
    else:
        assert kwargs == {"file": output}


@pytest.mark.asyncio
async def test_empty_sdk_result_reported_as_failure():
    client, inner = scoped([message(document())])
    inner.download_media.side_effect = None
    inner.download_media.return_value = None
    with pytest.raises(ValueError, match="did not return"):
        await client.download_attachment(CHAT, 10, BytesIO(), 100)
