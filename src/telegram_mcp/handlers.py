"""Text-only reads over a closed chat policy."""
from __future__ import annotations

from .core import AllowList, ChatNotAllowed, sanitize_text
from .client import marked_id as _marked_id
from .rich import rich_message_text

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def _display_name(entity, fallback: str = "") -> str:
    if entity is None:
        return fallback
    title = getattr(entity, "title", None)
    if title:
        return sanitize_text(title, limit=120)
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(p for p in parts if p).strip()
    return sanitize_text(name or getattr(entity, "username", None) or fallback, limit=120)


def _chat_ref(entry, entity) -> dict:
    return {"alias": entry.alias, "id": entry.id, "title": _display_name(entity, entry.title)}


def _message_to_dict(message) -> dict:
    date = getattr(message, "date", None)
    text = (getattr(message, "text", None) or getattr(message, "message", None)
            or rich_message_text(getattr(message, "rich_message", None)))
    return {
        "id": getattr(message, "id", None),
        "date": date.isoformat() if date is not None else None,
        "sender": _display_name(getattr(message, "sender", None)),
        "sender_id": getattr(message, "sender_id", None),
        "text": sanitize_text(text),
        "reply_to": getattr(message, "reply_to_msg_id", None),
    }


async def _messages_to_dicts(client, entity, messages) -> list[dict]:
    return [_message_to_dict(message) for message in messages]


def _clamp(limit, default: int = DEFAULT_LIMIT, maximum: int = MAX_LIMIT) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


async def _entity_of(client, allowlist: AllowList, chat):
    """Resolve a chat strictly through the allowlist and re-check the id we got back."""
    entry = allowlist.entry(chat)
    entity = await client.get_entity(entry.id)
    actual = _marked_id(entity)
    if actual != entry.id:
        raise ChatNotAllowed(
            f"Telegram returned chat id {actual} for {entry.alias!r}, which is not in the allowlist — "
            "request rejected."
        )
    return entry, entity


def _dialogs_by_id(dialogs) -> dict:
    return {_marked_id(getattr(dialog, "peer", None)): dialog for dialog in dialogs}


def _read_state(dialog) -> dict:
    """How much of a chat is unread, and the id of the last message read in it."""
    return {
        "unread_count": getattr(dialog, "unread_count", None),
        "read_inbox_max_id": getattr(dialog, "read_inbox_max_id", None),
    }


async def _dialogs_of(client, entities) -> dict:
    """The dialog of each chat, keyed by its marked id, in one request.

    Best-effort, like _reactor_names: when the batch fails — one chat Telegram
    refuses is enough — every chat is asked on its own, so the others keep their
    counts, and a chat that still fails is left without any.
    """
    entities = list(entities)
    if not entities:
        return {}
    try:
        return _dialogs_by_id(await client.get_peer_dialogs(entities))
    except Exception:
        if len(entities) == 1:
            return {}
    dialogs = []
    for entity in entities:
        try:
            dialogs.extend(await client.get_peer_dialogs([entity]))
        except Exception:
            continue
    return _dialogs_by_id(dialogs)


async def _read_boundary(client, entry, entity) -> dict:
    """The read state of one chat, for reading what follows it: no guessing here.

    Unlike _dialogs_of this is not best-effort — a failed request propagates as
    it is, and a chat without a dialog is a clear refusal, because any boundary
    made up in its place would silently skip or repeat messages.
    """
    dialog = _dialogs_by_id(await client.get_peer_dialogs([entity])).get(_marked_id(entity))
    state = _read_state(dialog)
    if state["read_inbox_max_id"] is None:
        raise ValueError(
            f"Telegram keeps no read position for {entry.alias!r} (a channel that was never joined?), "
            "so there is nothing to tell the unread messages by. Read with after_id or before_id instead."
        )
    return state


async def list_chats(client, allowlist: AllowList) -> dict:
    """Allowed chats together with their current titles and unread counts."""
    chats = []
    resolved = []
    for entry in allowlist.entries:
        item = {"alias": entry.alias, "id": entry.id, "title": entry.title}
        try:
            _, entity = await _entity_of(client, allowlist, entry.id)
        except Exception as exc:
            item["error"] = "The configured chat could not be read."
        else:
            item["title"] = _display_name(entity, entry.title)
            item["username"] = getattr(entity, "username", None)
            resolved.append((item, entity))
        chats.append(item)
    dialogs = await _dialogs_of(client, [entity for _, entity in resolved])
    for item, entity in resolved:
        item.update(_read_state(dialogs.get(_marked_id(entity))))
    return {"chats": chats}


async def get_chat_info(client, allowlist: AllowList, chat) -> dict:
    """Metadata of an allowed chat, the unread count and the read position included."""
    entry, entity = await _entity_of(client, allowlist, chat)
    dialogs = await _dialogs_of(client, [entity])
    return {
        "alias": entry.alias,
        "id": entry.id,
        "title": _display_name(entity, entry.title),
        "username": getattr(entity, "username", None),
        "participants_count": getattr(entity, "participants_count", None),
        **_read_state(dialogs.get(_marked_id(entity))),
    }


async def get_messages(
    client, allowlist: AllowList, chat, limit=DEFAULT_LIMIT, before_id=None, after_id=None, unread_only=False
) -> dict:
    """Messages of an allowed chat: the latest ones newest first, or onwards from an id.

    ``after_id`` reads forwards: the messages that follow it, oldest first.
    ``unread_only`` reads forwards from the chat's read position, and together
    with ``after_id`` from whichever of the two is further on — so paging on with
    ``next_after_id`` keeps working. A full forward page carries ``next_after_id``.
    Nothing is marked as read: the read position is only looked at.
    """
    after = None
    if after_id is not None:
        after = int(after_id)
        if after < 0:
            raise ValueError(f"after_id must be a message id (0 or more), got {after}.")
    entry, entity = await _entity_of(client, allowlist, chat)
    result = {"chat": _chat_ref(entry, entity)}
    limit = _clamp(limit)
    kwargs = {"limit": limit}
    if before_id:
        kwargs["max_id"] = int(before_id)
    if unread_only:
        state = await _read_boundary(client, entry, entity)
        result.update(state)
        after = max(after or 0, state["read_inbox_max_id"])
    if after is not None:
        # With reverse=True Telethon starts right after min_id and walks towards
        # the newest message; without it, min_id would only be where it stops,
        # and a page would hold the newest messages instead of the next ones.
        kwargs.update(min_id=after, reverse=True)
    messages = await client.get_messages(entity, **kwargs)
    result["messages"] = await _messages_to_dicts(client, entity, messages)
    if after is not None and len(messages) >= limit:
        result["next_after_id"] = messages[-1].id
    return result


async def get_message_context(client, allowlist: AllowList, chat, message_id, around=5) -> dict:
    """Messages around a specific one — enough to reconstruct a thread."""
    entry, entity = await _entity_of(client, allowlist, chat)
    around = _clamp(around, default=5, maximum=99)
    messages = await client.get_messages(
        entity, limit=around * 2 + 1, offset_id=int(message_id), add_offset=-around
    )
    return {
        "chat": _chat_ref(entry, entity),
        "target_id": int(message_id),
        "messages": await _messages_to_dicts(client, entity, messages),
    }


async def search_messages(client, allowlist: AllowList, query, chat=None, limit=DEFAULT_LIMIT) -> dict:
    """Full-text search: inside one allowed chat, or across all of them."""
    entries = [allowlist.entry(chat)] if chat is not None else list(allowlist.entries)
    limit = _clamp(limit)

    found = []
    errors = []
    for entry in entries:
        try:
            _, entity = await _entity_of(client, allowlist, entry.id)
            messages = await client.get_messages(entity, limit=limit, search=str(query))
        except ChatNotAllowed:
            raise
        except Exception as exc:
            errors.append({"alias": entry.alias, "error": "The configured chat could not be searched."})
            continue
        for item in await _messages_to_dicts(client, entity, messages):
            item["chat_alias"] = entry.alias
            item["chat_title"] = _display_name(entity, entry.title)
            found.append(item)

    found.sort(key=lambda m: m["date"] or "", reverse=True)
    result = {"query": sanitize_text(query, limit=200), "messages": found[:limit]}
    if errors:
        result["errors"] = errors
    return result

