"""Small adapter around the upstream WeChat 4.x database package."""

from __future__ import annotations

import xml.etree.ElementTree as ET


def open_db():
    # Imported only when the bot starts, so offline tests need no Windows client.
    from wechatauto.db import WeChatDB

    return WeChatDB()


def at_users(source: bytes | str | None) -> tuple[str, ...]:
    if not source:
        return ()
    if isinstance(source, bytes):
        if source.startswith(b"\x28\xb5\x2f\xfd"):
            import zstandard

            source = zstandard.ZstdDecompressor().decompress(source, max_output_size=1024 * 1024)
        source = source.decode("utf-8", "replace")
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return ()
    if root.tag != "msgsource":
        return ()
    return tuple(part.strip() for part in (root.findtext("atuserlist") or "").split(",") if part.strip())


def body(message: dict) -> str:
    content = str(message.get("content") or "")
    sender = str(message.get("sender_username") or "")
    return content[len(sender) + 2:] if sender and content.startswith(sender + ":\n") else content
