import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser, PeerChannel
from telethon.utils import get_peer_id

from telegram_mcp import handlers, server
from telegram_mcp.cli import build_parser
from telegram_mcp.client import ScopedClient, input_peer
from telegram_mcp.core import AllowList, ChatEntry, ChatNotAllowed, NotConfigured, load_allowlist

CHAT_A = -1000000000001
CHAT_B = -1000000000002
FORBIDDEN = -1000000000003


def policy():
    return AllowList([
        ChatEntry("first", CHAT_A, "First", "channel", 123),
        ChatEntry("second", CHAT_B, "Second", "channel", 456),
    ])


def record(chat_id=CHAT_A):
    return SimpleNamespace(
        id=10, chat_id=chat_id, text="allowed message", date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sender=SimpleNamespace(first_name="Alice"), sender_id=7, reply_to_msg_id=None,
    )


@pytest.mark.parametrize("peer,expected", [
    (ChatEntry("a", CHAT_A, "A", "channel", 9), (InputPeerChannel, 1, 9)),
    (ChatEntry("b", -25, "B", "chat"), (InputPeerChat, 25, None)),
    (ChatEntry("c", 31, "C", "user", 11), (InputPeerUser, 31, 11)),
])
def test_input_peers_are_fixed(peer, expected):
    result = input_peer(peer)
    assert isinstance(result, expected[0])
    assert get_peer_id(result) == peer.id
    assert getattr(result, "access_hash", None) == expected[2]


@pytest.mark.parametrize("data", [
    {}, {"chats": [], "local": []}, {"chats": "all"},
    {"chats": [{"id": CHAT_A, "peer_type": "channel"}]},
    {"chats": [{"id": CHAT_A, "peer_type": "channel", "access_hash": True}]},
    {"chats": [{"id": CHAT_A, "peer_type": "channel", "access_hash": 1, "wildcard": True}]},
    {"chats": [{"id": 12, "peer_type": "channel", "access_hash": 1}]},
])
def test_malformed_policy_fails_closed(tmp_path, data):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    with pytest.raises((ValueError, TypeError)):
        load_allowlist(path)


def test_local_additions_are_ignored(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    path.write_text('{"chats": []}')
    local = tmp_path / "local.json"
    local.write_text(json.dumps({"chats": [{"id": CHAT_A}]}))
    monkeypatch.setenv("TG_ALLOWED_CHATS_LOCAL_FILE", str(local))
    assert server.configure(path).entries == ()


@pytest.mark.asyncio
async def test_empty_policy_never_creates_client(monkeypatch):
    monkeypatch.setattr(server, "_allowlist", AllowList([]))
    monkeypatch.setattr(server, "_client", None)
    def forbidden_constructor(*args, **kwargs):
        pytest.fail("Telegram client must not be created")
    monkeypatch.setattr(server, "TelegramClient", forbidden_constructor)
    for operation in [server.list_chats(), server.search_messages("query")]:
        with pytest.raises(Exception, match="allowlist is empty"):
            await operation


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("get_chat_info", {"chat": str(FORBIDDEN)}),
    ("get_messages", {"chat": str(FORBIDDEN)}),
    ("get_message_context", {"chat": str(FORBIDDEN), "message_id": 1}),
    ("search_messages", {"chat": str(FORBIDDEN), "query": "secret"}),
])
async def test_forbidden_tool_denies_before_client(monkeypatch, tool, args):
    monkeypatch.setattr(server, "_allowlist", policy())
    fetch = AsyncMock(side_effect=AssertionError("client creation was reached"))
    monkeypatch.setattr(server, "_get_client", fetch)
    with pytest.raises(Exception, match="allowlist"):
        await getattr(server, tool)(**args)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_entity_cache_miss_does_not_enumerate():
    inner = SimpleNamespace(
        get_entity=AsyncMock(side_effect=ValueError("not found")), get_dialogs=AsyncMock(),
    )
    client = ScopedClient(inner, policy())
    with pytest.raises(ValueError, match="not found"):
        await client.get_entity(CHAT_A)
    inner.get_dialogs.assert_not_awaited()
    requested = inner.get_entity.call_args.args[0]
    assert isinstance(requested, InputPeerChannel)
    assert (requested.channel_id, requested.access_hash) == (1, 123)


@pytest.mark.asyncio
async def test_wrong_entity_identity_denied():
    inner = SimpleNamespace(get_entity=AsyncMock(return_value=PeerChannel(3)))
    with pytest.raises(ChatNotAllowed):
        await ScopedClient(inner, policy()).get_entity(CHAT_A)


@pytest.mark.asyncio
async def test_even_other_allowed_entity_cannot_replace_requested_entity():
    inner = SimpleNamespace(get_entity=AsyncMock(return_value=PeerChannel(2)))
    with pytest.raises(ChatNotAllowed):
        await ScopedClient(inner, policy()).get_entity(CHAT_A)


@pytest.mark.asyncio
async def test_message_result_from_other_chat_is_rejected():
    inner = SimpleNamespace(get_messages=AsyncMock(return_value=[record(FORBIDDEN)]))
    with pytest.raises(ChatNotAllowed):
        await ScopedClient(inner, policy()).get_messages(PeerChannel(1), limit=5)


@pytest.mark.asyncio
async def test_forbidden_direct_client_call_never_reaches_telegram():
    inner = SimpleNamespace(get_entity=AsyncMock(), get_messages=AsyncMock())
    client = ScopedClient(inner, policy())
    with pytest.raises(ChatNotAllowed):
        await client.get_entity(FORBIDDEN)
    with pytest.raises(ChatNotAllowed):
        await client.get_messages(PeerChannel(3), limit=5)
    inner.get_entity.assert_not_awaited()
    inner.get_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_search_is_individual_allowed_peer_searches():
    async def get_entity(peer):
        return PeerChannel(peer.channel_id)
    async def get_messages(peer, **kwargs):
        assert kwargs["search"] == "query"
        return [record(get_peer_id(peer))]
    inner = SimpleNamespace(
        get_entity=AsyncMock(side_effect=get_entity), get_messages=AsyncMock(side_effect=get_messages),
    )
    result = await handlers.search_messages(ScopedClient(inner, policy()), policy(), "query")
    assert len(result["messages"]) == 2
    assert {get_peer_id(call.args[0]) for call in inner.get_messages.call_args_list} == {CHAT_A, CHAT_B}
    assert {message["chat_alias"] for message in result["messages"]} == {"first", "second"}


@pytest.mark.asyncio
async def test_text_conversion_does_not_enrich_or_download():
    inner = SimpleNamespace(get_entity=AsyncMock(return_value=PeerChannel(1)),
                            get_messages=AsyncMock(return_value=[record()]))
    result = await handlers.get_messages(ScopedClient(inner, policy()), policy(), "first")
    assert result["messages"][0]["text"] == "allowed message"
    assert set(result["messages"][0]) == {"id", "date", "sender", "sender_id", "text", "reply_to"}


@pytest.mark.asyncio
async def test_five_read_tools_and_explicit_local_download():
    tools = await server.mcp.list_tools()
    assert {tool.name for tool in tools} == {
        "list_chats", "get_chat_info", "get_messages", "get_message_context", "search_messages",
        "download_attachment",
    }
    assert all(tool.annotations.read_only_hint == (tool.name != "download_attachment") for tool in tools)
    assert all(tool.annotations.destructive_hint is False for tool in tools)


@pytest.mark.parametrize("command", ["login", "dialogs", "download", "check"])
def test_runtime_cli_has_no_owner_commands(command):
    with pytest.raises(SystemExit):
        build_parser().parse_args([command])


@pytest.mark.asyncio
async def test_runtime_requires_local_proxy_before_connect(monkeypatch):
    monkeypatch.setattr(server, "_allowlist", policy())
    monkeypatch.setattr(server, "_client", None)
    for key in ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_STRING"]:
        monkeypatch.setenv(key, "1")
    monkeypatch.delenv("TG_LOCAL_PROXY_PORT", raising=False)
    with pytest.raises(NotConfigured, match="proxy"):
        await server._get_client()


def owner_setup_module():
    spec = importlib.util.spec_from_file_location("owner_setup", Path(__file__).parents[1] / "owner_setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_owner_file_private_and_not_overwritten(tmp_path):
    module = owner_setup_module()
    path = tmp_path / "credentials.json"
    module.write_private_json(path, {"synthetic": True})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        module.write_private_json(path, {"replacement": True})
    assert json.loads(path.read_text()) == {"synthetic": True}


def test_owner_selects_exact_groups_only():
    from telethon.tl.types import Chat
    module = owner_setup_module()
    def dialog(title, entity_id, group=True):
        entity = Chat(id=entity_id, title=title, photo=None, participants_count=3, date=None, version=1)
        return SimpleNamespace(name=title, is_group=group, entity=entity)
    records = [dialog("Project A", 4), dialog("Course B", 5),
               dialog("Private other", 6), dialog("Project A backup", 7), dialog("Project A", 8, False)]
    selected = module.select_chats(records, ["Project A", "Course B"])
    assert {entry["id"] for entry in selected["chats"]} == {-4, -5}
    with pytest.raises(ValueError, match="missing or ambiguous"):
        module.select_chats(records + [dialog("Project A", 9)], ["Project A"])
    with pytest.raises(ValueError, match="missing or ambiguous"):
        module.select_chats(records, ["Missing"])
