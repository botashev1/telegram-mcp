"""Attachment descriptions derived from message data, without network requests."""
from __future__ import annotations

from telethon.tl import types

from .core import sanitize_text


def _byte_size(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def photo_size(photo):
    """Choose a static photo size; Telegram can also attach a larger video."""
    candidates = []
    for size in photo.sizes:
        if isinstance(size, types.PhotoSize):
            length = _byte_size(size.size)
        elif isinstance(size, types.PhotoSizeProgressive):
            lengths = [_byte_size(value) for value in size.sizes]
            length = max(lengths) if lengths and all(value is not None for value in lengths) else None
        else:
            continue
        if length is not None and length > 0:
            candidates.append((length, size))
    return max(candidates, key=lambda item: item[0]) if candidates else (None, None)


def attachment_metadata(message) -> dict | None:
    """Return public metadata only for directly attached Telegram documents/photos.

    Filenames are display labels, never paths. Secret, disappearing and protected
    attachments are not offered for download. Web previews and arbitrary URLs
    are deliberately not interpreted as attachments.
    """
    media = getattr(message, "media", None)
    blocked = bool(getattr(message, "noforwards", False)) or getattr(media, "ttl_seconds", None) is not None
    if isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
        document = media.document
        name = next((attribute.file_name for attribute in document.attributes
                     if isinstance(attribute, types.DocumentAttributeFilename)), None)
        if name:
            name = sanitize_text(name.replace("\\", "/").rsplit("/", 1)[-1], limit=200)
        size = _byte_size(document.size)
        return {
            "kind": "document", "file_name": name or None,
            "mime_type": sanitize_text(document.mime_type, limit=100),
            "size": size, "downloadable": not blocked and size is not None and size > 0,
        }
    if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
        size, selected = photo_size(media.photo)
        return {
            "kind": "photo", "file_name": None, "mime_type": "image/jpeg",
            "size": size, "downloadable": not blocked and selected is not None,
        }
    return None
