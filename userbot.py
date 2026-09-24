"""Telethon userbot: read-only access to history and media of chats the bot is not a member of."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

from . import safety

logger = logging.getLogger(__name__)

T = TypeVar("T")

API_ID_ENV = "TELEGRAM_USERBOT_API_ID"
API_HASH_ENV = "TELEGRAM_USERBOT_API_HASH"
MAX_PAGE = 30
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_MEDIA_SECONDS = 15 * 60


class UserbotError(RuntimeError):
    """Userbot is unavailable or the request cannot be served; message is user-facing."""


@dataclass(frozen=True)
class Credentials:
    api_id: int
    api_hash: str
    session_path: Path

    @property
    def session_file(self) -> Path:
        return self.session_path.with_suffix(".session")


def load_credentials() -> Optional[Credentials]:
    """Read credentials in the CALLER's profile scope (secrets and HERMES_HOME are scope-bound)."""
    from gateway.platforms._shared import get_scoped_secret
    from hermes_constants import get_hermes_home

    api_id = str(get_scoped_secret(API_ID_ENV, "") or "").strip()
    api_hash = str(get_scoped_secret(API_HASH_ENV, "") or "").strip()
    if not api_id.isdigit() or not api_hash:
        return None
    return Credentials(int(api_id), api_hash, get_hermes_home() / "auth" / "userbot")


def telethon_available() -> bool:
    try:
        import telethon  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True)
class MediaInfo:
    kind: str  # photo | image | voice | audio | video_note | video | document
    label: str
    duration: int = 0
    file_name: str = ""
    mime_type: str = ""

    @property
    def is_image(self) -> bool:
        return self.kind in ("photo", "image")

    @property
    def has_speech(self) -> bool:
        return self.kind in ("voice", "audio", "video_note", "video")

    def as_record(self) -> Dict[str, Any]:
        return {"kind": self.kind, "duration": self.duration, "file_name": self.file_name}


def describe_media(msg: Any) -> Optional[MediaInfo]:
    from telethon.tl.types import (
        DocumentAttributeAudio, DocumentAttributeFilename, DocumentAttributeVideo,
        MessageMediaDocument, MessageMediaPhoto,
    )

    media = getattr(msg, "media", None)
    if isinstance(media, MessageMediaPhoto):
        return MediaInfo("photo", "Фото")
    if not isinstance(media, MessageMediaDocument) or media.document is None:
        return None
    doc = media.document
    mime = getattr(doc, "mime_type", "") or ""
    file_name, audio, video = "", None, None
    for attr in doc.attributes or []:
        if isinstance(attr, DocumentAttributeAudio):
            audio = attr
        elif isinstance(attr, DocumentAttributeVideo):
            video = attr
        elif isinstance(attr, DocumentAttributeFilename):
            file_name = attr.file_name
    if audio is not None and not video:
        duration = int(audio.duration or 0)
        if audio.voice:
            return MediaInfo("voice", "Голосовое", duration, file_name, mime)
        return MediaInfo("audio", "Аудио", duration, file_name, mime)
    if video is not None:
        duration = int(video.duration or 0)
        if video.round_message:
            return MediaInfo("video_note", "Кружок", duration, file_name, mime)
        return MediaInfo("video", "Видео", duration, file_name, mime)
    if mime.startswith("image/"):
        return MediaInfo("image", "Изображение", 0, file_name, mime)
    return MediaInfo("document", "Файл", 0, file_name, mime)


def _sender_name(sender: Any) -> str:
    if sender is None:
        return "Неизвестный"
    title = getattr(sender, "title", None)
    if title:
        return title
    name = " ".join(p for p in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if p)
    username = getattr(sender, "username", None)
    handle = f"@{username}" if username else f"id={sender.id}"
    return f"{name} ({handle})" if name else handle


def message_record(msg: Any, caller_id: Optional[str], *, target_id: Optional[int] = None,
                   request_id: Optional[int] = None, full: bool = False) -> Dict[str, Any]:
    from telethon.tl.types import MessageEntitySpoiler

    media = describe_media(msg)
    spoiler = bool(getattr(getattr(msg, "media", None), "spoiler", False)) or any(
        isinstance(e, MessageEntitySpoiler) for e in getattr(msg, "entities", None) or ())
    return safety.chat_message(
        msg.id, text=getattr(msg, "message", None) or "", sender=_sender_name(getattr(msg, "sender", None)),
        date=msg.date, media=media.as_record() if media else None,
        reply_to=getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None),
        from_caller=bool(caller_id) and str(getattr(msg, "sender_id", "")) == str(caller_id),
        target=msg.id == target_id, request=msg.id == request_id, full=full, spoiler=spoiler)


class Userbot:
    """A single Telethon client on a dedicated loop thread.

    Telethon binds its connection to the loop it was created on, while callers are the gateway
    loop, sync tool threads and the tool registry's own loops.
    """

    def __init__(self, on_status: Optional[Callable[[dict], None]] = None) -> None:
        self._on_status = on_status
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_lock = threading.Lock()
        self._client: Any = None
        self._client_creds: Optional[Credentials] = None
        self._connect_lock: Optional[asyncio.Lock] = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._loop is None or self._loop.is_closed():
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="tg-ghost-userbot", daemon=True).start()
                self._loop = loop
            return self._loop

    def _submit(self, fn: Callable[..., Awaitable[T]], args: tuple):
        if not telethon_available():
            raise UserbotError("Telethon не установлен.")
        creds = load_credentials()
        if creds is None:
            raise UserbotError(f"Юзербот не настроен: задайте {API_ID_ENV} и {API_HASH_ENV}.")
        return asyncio.run_coroutine_threadsafe(self._with_client(creds, fn, args), self._ensure_loop())

    def call(self, fn: Callable[..., Awaitable[T]], *args: Any, timeout: float = 120) -> T:
        """Run ``fn(client, *args)`` on the userbot loop, blocking the calling thread."""
        return self._submit(fn, args).result(timeout=timeout)

    async def acall(self, fn: Callable[..., Awaitable[T]], *args: Any, timeout: float = 120) -> T:
        """Run ``fn(client, *args)`` on the userbot loop from any other event loop."""
        return await asyncio.wait_for(asyncio.wrap_future(self._submit(fn, args)), timeout)

    async def _with_client(self, creds: Credentials, fn: Callable[..., Awaitable[T]], args: tuple) -> T:
        try:
            client = await self._connected(creds)
        except Exception as exc:
            self._report(state="error", error=str(exc))
            raise
        return await fn(client, *args)

    def _report(self, **status: Any) -> None:
        if self._on_status is None:
            return
        try:
            self._on_status({**status, "at": int(time.time())})
        except Exception:
            logger.debug("[tg-ghost] userbot status report failed", exc_info=True)

    async def _connected(self, creds: Credentials) -> Any:
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:
            if self._client is not None and self._client_creds == creds and self._client.is_connected():
                return self._client
            await self._disconnect()
            if not creds.session_file.exists():
                raise UserbotError("Сессия юзербота не найдена: выполните `hermes ghost login`.")
            from telethon import TelegramClient

            client = TelegramClient(str(creds.session_path), creds.api_id, creds.api_hash)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise UserbotError("Сессия юзербота не авторизована: выполните `hermes ghost login`.")
            self._client, self._client_creds = client, creds
            me = await client.get_me()
            logger.info("[tg-ghost] userbot connected as id=%s", me.id)
            self._report(state="connected", user=f"@{me.username}" if me.username else str(me.id))
            return client

    async def _disconnect(self) -> None:
        client, self._client, self._client_creds = self._client, None, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()

    def shutdown(self) -> None:
        with self._loop_lock:
            loop, self._loop = self._loop, None
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self._disconnect(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)


async def _entity(client: Any, chat_id: int) -> Any:
    try:
        return await client.get_input_entity(chat_id)
    except ValueError:
        # A fresh session knows only cached peers; the dialog list fills the cache.
        await client.get_dialogs()
        try:
            return await client.get_input_entity(chat_id)
        except ValueError:
            raise UserbotError(f"Юзербот не видит чат {chat_id}: он должен состоять в нём.") from None


async def fetch_window(client: Any, chat_id: int, caller_id: str, request_id: int,
                       target_id: Optional[int], limit: int) -> List[Dict[str, Any]]:
    """Messages up to the target (or the request when there is none) inclusive; around a target
    half of the window follows it."""
    entity = await _entity(client, chat_id)
    anchor = target_id or request_id
    after = limit // 2 if target_id else 0
    older = await client.get_messages(entity, offset_id=anchor + 1, limit=limit - after)
    newer = await client.get_messages(entity, offset_id=anchor, limit=after, reverse=True) if after else []
    messages = sorted({m.id: m for m in [*older, *newer] if m}.values(), key=lambda m: m.id)
    return [message_record(m, caller_id, target_id=target_id, request_id=request_id) for m in messages]


async def fetch_page(client: Any, chat_id: int, caller_id: str, offset_id: int, direction: str,
                     limit: int) -> List[Dict[str, Any]]:
    entity = await _entity(client, chat_id)
    if direction == "after":
        messages = await client.get_messages(entity, offset_id=offset_id, limit=limit, reverse=True)
    else:
        messages = await client.get_messages(entity, offset_id=offset_id, limit=limit)
    return [message_record(m, caller_id) for m in sorted(messages, key=lambda m: m.id)]


async def fetch_messages(client: Any, chat_id: int, caller_id: str, ids: List[int]) -> List[Dict[str, Any]]:
    """Specific messages with their full, untruncated text."""
    entity = await _entity(client, chat_id)
    messages = await client.get_messages(entity, ids=ids)
    return [message_record(m, caller_id, full=True) for m in messages if m is not None]


async def download_media(client: Any, chat_id: int, message_id: int) -> tuple[bytes, MediaInfo, str]:
    """Media bytes, their description and the sender id."""
    entity = await _entity(client, chat_id)
    msg = await client.get_messages(entity, ids=message_id)
    if msg is None:
        raise UserbotError(f"Сообщение #{message_id} не найдено.")
    media = describe_media(msg)
    if media is None:
        raise UserbotError(f"В сообщении #{message_id} нет медиа.")
    # Downloads land in memory; anyone in the chat can post a huge file and ask for it.
    size = getattr(msg.file, "size", None) or 0
    if size > MAX_MEDIA_BYTES:
        raise UserbotError(f"Медиа в сообщении #{message_id} больше {MAX_MEDIA_BYTES // 2**20} МБ.")
    if media.duration > MAX_MEDIA_SECONDS:
        raise UserbotError(f"Медиа в сообщении #{message_id} длиннее {MAX_MEDIA_SECONDS // 60} минут.")
    data = await client.download_media(msg, file=bytes)
    if not data:
        raise UserbotError(f"Не удалось скачать медиа из сообщения #{message_id}.")
    return bytes(data), media, str(getattr(msg, "sender_id", ""))
