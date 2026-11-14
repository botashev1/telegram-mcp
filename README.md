# Local Telegram read-only service

Derived from nskondratev/telegram-mcp 0.7.0, commit
700f708790bd929828f53ae4f3b9d7046810b859, with a reduced local surface.

Runtime command: `telegram-read-only --allowlist /owner/policy.json serve`.
Five reading tools are registered: `list_chats`, `get_chat_info`, `get_messages`,
`get_message_context`, `search_messages`. Messages with directly attached documents
or photos include an `attachment` object (kind, filename, MIME type, size in bytes,
and whether it is downloadable). Reading messages never downloads their files.

`download_attachment(chat, message_id)` downloads one explicitly selected attachment
to an owner-configured local cache. It returns a generated absolute path, the actual
byte count, SHA-256 and attachment metadata. There is no runtime login, global
dialog enumeration, or Telegram sending/editing/deletion command.

## Attachments

Documents (including audio/video files) and static photos are supported. Albums can
be downloaded one message at a time. Web previews, arbitrary URLs, contacts,
disappearing attachments and messages marked as protected are not downloadable.
Every download re-fetches its message using the configured peer and verifies both
the chat and message IDs before transferring bytes. Session keys, access hashes
and Telegram file references are not returned.

The owner must set `TG_DOWNLOAD_DIR` before starting the service; without it,
downloads fail closed and existing reading tools remain available. The directory
must already exist, be canonical and service-owned, and not be writable by other
users. The tool cannot select a destination or supply an external URL. See the
[macOS updater](deployment/macos/README.md) for the protected local installation.

Defaults are 50 MiB per file, 250 MiB or 100 files in the cache, one download at a
time across service processes, and a 120-second timeout. The service checks both
declared and actual size and refuses incomplete transfers. It uses generated
filenames, refuses symlinks/hard links, never overwrites a completed download, and
removes partial files on errors or cancellation. A killed process can leave a
partial file: it counts toward quota until the owner removes it.

Completed downloads persist until owner cleanup; there is no automatic deletion
or eviction. The quota error requests cleanup rather than removing files another
task may still need. Files are not executed, unpacked, parsed or transcribed by
this service. Such processing is a separate step with its own permissions.

Downloading writes a local file, so its MCP `readOnlyHint` is **false**. Its
`destructiveHint` is false. The five existing reading tools retain their original
read-only annotations; nothing marks Telegram messages read.

## Owner policy

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

The broker must run under a separate OS identity. Keep source and policy
unwritable by the agent, session and credentials unreadable by it, and expose only the fixed
stdio service entry point. The Telegram session still has full Telegram account
authority; these source changes do not change Telegram account permissions.
Messages returned through MCP are sent to the chosen model provider. The code
itself has no extra analytics or relay service. Dependencies and transport
must be pinned and inspected separately.

## Development and review

Install with `python -m pip install -e '.[dev]'`, then run `pytest -q` and
`ruff check .`. Tests use synthetic peers, metadata and streams; they do not
require credentials or connect to Telegram. The SDK tests exercise real Telethon
download code with an offline transport substitute.

`codex/telegram-readonly-baseline` records the pre-existing hardened runtime.
Review attachment changes against that branch, not upstream `main`; see
[baseline provenance](LOCAL_BASELINE.md). The upstream project does not yet have
this local isolation layout, so an upstream PR must account for that dependency.
No credentials, account policy, downloaded attachments or chat histories belong
in this repository.
