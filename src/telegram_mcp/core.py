"""Closed, owner-managed chat policy and output sanitizing."""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TEXT_LIMIT = 4000
ALLOWLIST_ENV = "TG_ALLOWED_CHATS_FILE"
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


class ChatNotAllowed(Exception):
    pass


class NotConfigured(Exception):
    pass


def default_allowlist_path() -> Path:
    configured = os.environ.get(ALLOWLIST_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "telegram-mcp" / "allowed_chats.json"


def normalize_chat_id(value) -> int:
    if isinstance(value, bool):
        raise ValueError("chat id must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        return int(value.strip())
    raise ValueError("chat id must be an integer")


@dataclass(frozen=True)
class ChatEntry:
    alias: str
    id: int
    title: str
    peer_type: str
    access_hash: int | None = None


class AllowList:
    def __init__(self, entries: list[ChatEntry]):
        self.entries = tuple(entries)
        self._by_id = {entry.id: entry for entry in entries}
        self._by_alias = {entry.alias.casefold(): entry for entry in entries}
        self._by_title = {entry.title.casefold(): entry for entry in entries if entry.title}
        if len(self._by_id) != len(entries) or len(self._by_alias) != len(entries):
            raise ValueError("duplicate chat id or alias")
        if len(self._by_title) != sum(bool(entry.title) for entry in entries):
            raise ValueError("duplicate chat title")

    def require_nonempty(self) -> None:
        if not self.entries:
            raise NotConfigured("The allowlist is empty; Telegram access is disabled.")

    def entry(self, ref) -> ChatEntry:
        try:
            found = self._by_id.get(normalize_chat_id(ref))
        except ValueError:
            key = str(ref).strip().casefold()
            found = self._by_alias.get(key) or self._by_title.get(key)
        if found is None:
            raise ChatNotAllowed("Chat is not in the owner-managed allowlist; access denied.")
        return found

    def contains(self, chat_id) -> bool:
        try:
            return normalize_chat_id(chat_id) in self._by_id
        except ValueError:
            return False


def load_allowlist(path) -> AllowList:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"chats"}:
        raise ValueError("allowlist must contain only a chats array")
    raw_chats = data["chats"]
    if not isinstance(raw_chats, list):
        raise ValueError("chats must be an array")
    entries = []
    for raw in raw_chats:
        if not isinstance(raw, dict) or set(raw) - {"id", "alias", "title", "peer_type", "access_hash"}:
            raise ValueError("invalid chat entry keys")
        chat_id = normalize_chat_id(raw.get("id"))
        peer_type = raw.get("peer_type")
        access_hash = raw.get("access_hash")
        if peer_type not in {"channel", "user", "chat"}:
            raise ValueError("peer_type must be channel, user or chat")
        if peer_type == "channel" and not -2000000000000 < chat_id < -1000000000000:
            raise ValueError("channel id must use Telegram's -100 marked format")
        if peer_type == "user" and not 0 < chat_id < 1000000000000:
            raise ValueError("user id must be positive")
        if peer_type == "chat" and not -1000000000000 < chat_id < 0:
            raise ValueError("basic group id must be negative")
        if peer_type != "chat" and (isinstance(access_hash, bool) or not isinstance(access_hash, int)
                                      or not -(2**63) <= access_hash < 2**63):
            raise ValueError("user/channel requires an exact signed 64-bit access_hash")
        if peer_type == "chat" and access_hash is not None:
            raise ValueError("basic groups do not use an access_hash")
        alias = raw.get("alias", str(chat_id))
        title = raw.get("title", "")
        if not isinstance(alias, str) or not alias.strip() or not isinstance(title, str):
            raise ValueError("alias and title must be strings; alias cannot be empty")
        entries.append(ChatEntry(alias.strip(), chat_id, title.strip(), peer_type, access_hash))
    return AllowList(entries)


def sanitize_text(text, limit: int = DEFAULT_TEXT_LIMIT) -> str:
    if not text:
        return ""
    text = _INVISIBLE.sub("", str(text))
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf"))
    return text[:limit] + ("…" if len(text) > limit else "")
