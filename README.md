# Local Telegram read-only service

Derived from nskondratev/telegram-mcp 0.7.0, commit
700f708790bd929828f53ae4f3b9d7046810b859, with a reduced local surface.

Runtime command: `telegram-read-only --allowlist /owner/policy.json serve`.
Only five text tools are registered: list_chats, get_chat_info, get_messages,
get_message_context, search_messages. There is no runtime login, dialogs,
download, reactions, poll-voters or write command.

Owner policy format:

```json
{"chats": [{"id": -1001234567890, "alias": "work", "title": "Work",
            "peer_type": "channel", "access_hash": 123456789}]}
```

The IDs and access hashes above are examples. Real values are supplied only by
owner setup. `peer_type` is `channel`, `chat` (basic group) or `user`;
basic groups have no access_hash. Empty policy disables Telegram access before
client creation. Missing/malformed policy fails startup. There are no local
policy additions or runtime username lookups. A cache miss fails; it never
triggers enumeration. Search iterates the configured peers only.

The broker must run under a separate OS identity. Keep source, policy, session
and credentials unreadable/unwritable by the agent and expose only the fixed
stdio service entry point. The Telegram session still has full Telegram account
authority; these source changes do not change Telegram account permissions.
Messages returned through MCP are sent to the chosen model provider. The code
itself has no extra analytics or relay service. Dependencies and transport
must be pinned and inspected separately.
