"""Only explicitly configured Telegram peers can reach the underlying client."""
from __future__ import annotations

from .attachments import AttachmentError
from .core import AllowList, ChatNotAllowed
from .media import attachment_metadata, photo_size


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

    async def download_attachment(self, chat_id: int, message_id: int, output, max_bytes: int) -> dict:
        """Download one freshly checked message into a caller-owned bounded stream."""
        entry = self._allowlist.entry(chat_id)
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise AttachmentError("message_id must be a positive integer.")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise AttachmentError("The attachment size limit must be a positive integer.")
        if not callable(getattr(output, "write", None)):
            raise AttachmentError("Attachments require a bounded output stream, not a file path.")
        # Telethon's ids= path uses account-wide GetMessages for basic groups
        # and users, filtering the peer only after receiving its response.
        # A narrow history window keeps the Telegram request peer-scoped.
        messages = await self._inner.get_messages(
            input_peer(entry), limit=1, min_id=message_id - 1, max_id=message_id + 1,
        )
        if not messages or len(messages) != 1 or messages[0] is None:
            raise AttachmentError("The requested attachment message is unavailable.")
        message = messages[0]
        if getattr(message, "chat_id", None) != entry.id or getattr(message, "id", None) != message_id:
            raise ChatNotAllowed("Telegram returned an unexpected attachment message identity.")
        metadata = attachment_metadata(message)
        if metadata is None or not metadata["downloadable"]:
            raise AttachmentError("The message has no supported downloadable attachment.")
        if metadata["size"] > max_bytes:
            raise AttachmentError("The attachment exceeds the configured size limit.")
        kwargs = {"file": output}
        if metadata["kind"] == "photo":
            _, selected = photo_size(message.media.photo)
            kwargs["thumb"] = selected.type
        # Passing Message would enable Telethon's automatic file-reference
        # refresh via get_messages(ids=...). Use only the checked media so an
        # expired reference fails safely; the next explicit call refetches it
        # through the peer-scoped history request above.
        result = await self._inner.download_media(message.media, **kwargs)
        if result is None:
            raise AttachmentError("Telegram did not return the requested attachment.")
        return metadata
