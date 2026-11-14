from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from telethon.tl import types

from telegram_mcp import handlers, server
from telegram_mcp.core import AllowList, ChatEntry


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_allowlist", AllowList([ChatEntry("allowed", -25, "Allowed", "chat")]))
    monkeypatch.setenv("TG_DOWNLOAD_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_tool_validates_peer_and_downloads_to_configured_cache(monkeypatch, configured):
    async def download(chat_id, message_id, output, limit):
        assert (chat_id, message_id) == (-25, 42)
        assert limit == 50 * 1024 * 1024
        output.write(b"%PDF-example")
        return {"file_name": "assignment.pdf", "mime_type": "application/pdf", "size": 12}
    client = SimpleNamespace(download_attachment=AsyncMock(side_effect=download))
    monkeypatch.setattr(server, "_get_client", AsyncMock(return_value=client))
    result = await server.download_attachment("allowed", 42)
    assert Path(result["path"]).parent == configured
    assert Path(result["path"]).read_bytes() == b"%PDF-example"
    assert result["attachment"]["file_name"] == "assignment.pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize("chat,message_id", [("forbidden", 42), ("allowed", 0), ("allowed", True)])
async def test_invalid_request_never_connects(monkeypatch, configured, chat, message_id):
    fetch = AsyncMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(server, "_get_client", fetch)
    with pytest.raises(ToolError):
        await server.download_attachment(chat, message_id)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfigured_download_never_connects(monkeypatch, configured):
    monkeypatch.delenv("TG_DOWNLOAD_DIR")
    fetch = AsyncMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(server, "_get_client", fetch)
    with pytest.raises(Exception, match="downloads are not configured"):
        await server.download_attachment("allowed", 42)
    fetch.assert_not_awaited()


def test_message_metadata_is_additive_and_does_not_download():
    document = types.Document(id=1, access_hash=2, file_reference=b"synthetic", date=None,
                              mime_type="application/pdf", size=12, dc_id=2,
                              attributes=[types.DocumentAttributeFilename("assignment.pdf")])
    message = SimpleNamespace(id=42, text="caption", media=types.MessageMediaDocument(document=document))
    result = handlers._message_to_dict(message)
    assert result["text"] == "caption"
    assert result["attachment"] == {
        "kind": "document", "file_name": "assignment.pdf", "mime_type": "application/pdf",
        "size": 12, "downloadable": True,
    }
    assert "file_reference" not in str(result)
