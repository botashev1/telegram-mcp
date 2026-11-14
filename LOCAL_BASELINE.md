# Local hardened baseline

Upstream: https://github.com/nskondratev/telegram-mcp

Pinned upstream commit: `700f708790bd929828f53ae4f3b9d7046810b859`.

This baseline records the pre-existing locally hardened runtime, installed on 2026-11-14, before attachment support. It uses a dedicated OS identity, an owner-managed peer allowlist, text-only MCP tools and a Telegram-only network proxy. Source files were copied from the installed runtime; earlier offline hardening tests were recovered from the setup workspace. Credentials, actual chat policies and conversations are excluded.

The following attachment commit is reviewable separately against this baseline. An upstream PR would also need to account for these existing hardening changes; they were not introduced by attachment support.
