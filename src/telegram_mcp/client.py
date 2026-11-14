"""Only explicitly configured Telegram peers can reach the underlying client."""
from __future__ import annotations

from .core import AllowList, ChatNotAllowed


def marked_id(entity):
    from telethon.utils import get_peer_id

    return get_peer_id(entity)


def input_peer(entry):
    from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser

    if entry.peer_type == "channel":
        return InputPeerChannel(-entry.id - 1000000000000, entry.access_hash)
    if entry.peer_type == "user":
        return InputPeerUser(entry.id, entry.access_hash)
    return InputPeerChat(-entry.id)


class ScopedClient:
    def __init__(self, inner, allowlist: AllowList):
        self._inner = inner
        self._allowlist = allowlist

    def _peer(self, entity):
        return input_peer(self._allowlist.entry(marked_id(entity)))

    async def get_entity(self, chat_id):
        entry = self._allowlist.entry(chat_id)
        entity = await self._inner.get_entity(input_peer(entry))
        if marked_id(entity) != entry.id:
            raise ChatNotAllowed("Telegram returned an unexpected chat identity.")
        return entity

    async def get_messages(self, entity, **kwargs):
        peer = self._peer(entity)
        expected_id = marked_id(peer)
        messages = await self._inner.get_messages(peer, **kwargs)
        if any(getattr(message, "chat_id", None) != expected_id for message in messages):
            raise ChatNotAllowed("Telegram returned messages from an unexpected chat.")
        return messages

    async def get_peer_dialogs(self, entities):
        from telethon.tl.functions.messages import GetPeerDialogsRequest

        peers = [self._peer(entity) for entity in entities]
        expected_ids = {marked_id(peer) for peer in peers}
        if not peers:
            return []
        answer = await self._inner(GetPeerDialogsRequest(peers=peers))
        dialogs = list(getattr(answer, "dialogs", None) or [])
        if any(marked_id(dialog.peer) not in expected_ids for dialog in dialogs):
            raise ChatNotAllowed("Telegram returned an unexpected chat dialog.")
        return dialogs
