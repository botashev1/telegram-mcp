import json
import os
import select
import subprocess
import sys
import time

import pytest


def rpc(tmp_path, method, params, empty=False):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"chats": [] if empty else [{
        "id": -1000000000001, "alias": "permitted", "title": "Synthetic",
        "peer_type": "channel", "access_hash": 123,
    }]}))
    env = {key: value for key, value in os.environ.items() if not key.startswith(("TELEGRAM_", "TG_"))}
    env["HOME"] = str(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "telegram_mcp", "--allowlist", str(policy_path), "serve"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "offline-test", "version": "1"},
        }},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
    ]
    try:
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            line = process.stdout.readline()
            if not line:
                raise AssertionError("MCP process exited without a reply")
            response = json.loads(line)
            if response.get("id") == 2:
                return response
        raise AssertionError("MCP reply timed out")
    finally:
        process.kill()
        process.wait(timeout=5)


def test_stdio_exposes_five_reads_and_local_download(tmp_path):
    response = rpc(tmp_path, "tools/list", {})
    tools = response["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "list_chats", "get_chat_info", "get_messages", "get_message_context", "search_messages",
        "download_attachment",
    }
    assert all(
        tool["annotations"]["readOnlyHint"] == (tool["name"] != "download_attachment") for tool in tools
    )
    download = next(tool for tool in tools if tool["name"] == "download_attachment")
    assert set(download["inputSchema"]["properties"]) == {"chat", "message_id"}


@pytest.mark.parametrize("tool,arguments", [
    ("get_chat_info", {"chat": "-1000000000009"}),
    ("get_messages", {"chat": "-1000000000009"}),
    ("get_message_context", {"chat": "-1000000000009", "message_id": 10}),
    ("search_messages", {"chat": "-1000000000009", "query": "anything"}),
    ("download_attachment", {"chat": "-1000000000009", "message_id": 10}),
])
def test_stdio_forbidden_chat_refused_without_credentials(tmp_path, tool, arguments):
    response = rpc(tmp_path, "tools/call", {"name": tool, "arguments": arguments})
    text = json.dumps(response)
    assert "allowlist" in text
    assert "credentials" not in text
    assert response["result"]["isError"] is True


@pytest.mark.parametrize("tool,args", [("list_chats", {}), ("search_messages", {"query": "anything"})])
def test_stdio_empty_policy_does_not_connect(tmp_path, tool, args):
    response = rpc(tmp_path, "tools/call", {"name": tool, "arguments": args}, empty=True)
    assert "allowlist is empty" in json.dumps(response)
    assert "credentials" not in json.dumps(response)
    assert response["result"]["isError"] is True


def test_stdio_allowed_chat_requires_owner_credentials(tmp_path):
    response = rpc(tmp_path, "tools/call", {"name": "get_messages", "arguments": {"chat": "permitted"}})
    assert "credentials are not configured" in json.dumps(response)
    assert response["result"]["isError"] is True


def test_stdio_write_tool_does_not_exist(tmp_path):
    response = rpc(tmp_path, "tools/call", {
        "name": "send_message", "arguments": {"chat": "permitted", "text": "x"},
    })
    assert response["result"]["isError"] is True


def test_stdio_download_requires_owner_config_before_credentials(tmp_path):
    response = rpc(tmp_path, "tools/call", {
        "name": "download_attachment", "arguments": {"chat": "permitted", "message_id": 10},
    })
    assert "downloads are not configured" in json.dumps(response)
    assert "credentials" not in json.dumps(response)
    assert response["result"]["isError"] is True
