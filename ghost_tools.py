"""Agent tools for guest turns (chat history, full message texts, on-demand media understanding)
and the guard that keeps guest turns to their toolsets."""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Optional

from tools.registry import no_cache_check_fn, tool_error, tool_result

from . import safety
from . import userbot as ub
from .adapter import PLATFORM, RESET_REQUESTED_KEY, live_adapter, toolsets_filter

logger = logging.getLogger(__name__)

TOOLSET = "tg-ghost"
HISTORY_COUNTER_KEY = "ghost_history_fetched"
MAX_FULL_MESSAGES = 10
# Hermes' deferred-tool bridge: they only read schemas and belong to no toolset. Their sibling
# tool_call needs no entry, Hermes unwraps it to the target tool before pre_tool_call runs.
_SCHEMA_LOOKUP_TOOLS = frozenset({"tool_search", "tool_describe"})


def _session(name: str) -> str:
    from gateway.session_context import get_session_env
    return get_session_env(f"HERMES_SESSION_{name}", "") or ""


@no_cache_check_fn
def in_ghost_session() -> bool:
    return _session("PLATFORM") == PLATFORM


def _chat_id() -> int:
    if not in_ghost_session():
        raise ub.UserbotError("Инструмент доступен только в гостевом режиме Telegram.")
    return int(_session("CHAT_ID"))


def _int_arg(args: dict, name: str) -> int:
    try:
        return int(args[name])
    except (KeyError, TypeError, ValueError):
        raise ub.UserbotError(f"Параметр {name} обязателен и должен быть числом.") from None


def _cache_media(data: bytes, media: ub.MediaInfo) -> str:
    from gateway.platforms.base import cache_audio_from_bytes, cache_image_from_bytes, cache_video_from_bytes

    # The file name is chosen by whoever posted the file.
    suffix = Path(media.file_name).suffix.lower()
    suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix) else ""
    if media.is_image:
        return cache_image_from_bytes(data, suffix or ".jpg")
    if media.kind in ("video_note", "video"):
        return cache_video_from_bytes(data, ".mp4")
    return cache_audio_from_bytes(data, suffix or ".ogg")


def _guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    def handler(args: dict, **kwargs: Any) -> Any:
        try:
            return fn(args or {}, **kwargs)
        except ub.UserbotError as exc:
            return tool_error(str(exc))
        except Exception as exc:
            logger.error("[tg-ghost] %s failed: %s", fn.__name__, exc, exc_info=True)
            return tool_error(f"{type(exc).__name__}: {exc}")
    handler.__name__ = fn.__name__
    return handler


class GhostTools:
    def __init__(self, ctx: Any, userbot: ub.Userbot):
        self._ctx = ctx
        self._userbot = userbot

    def register(self) -> None:
        for name, schema, handler, emoji in (
            ("ghost_history", HISTORY_SCHEMA, self.get_history, "📜"),
            ("ghost_messages", MESSAGES_SCHEMA, self.get_messages, "📄"),
            ("ghost_media", MEDIA_SCHEMA, self.media, "🎬"),
            ("ghost_reset", RESET_SCHEMA, self.reset, "🔄"),
        ):
            self._ctx.register_tool(
                name=name, toolset=TOOLSET, schema={"name": name, **schema}, handler=_guarded(handler),
                check_fn=in_ghost_session, description=schema["description"], emoji=emoji)
        self._ctx.register_hook("pre_tool_call", self.guard)

    def guard(self, tool_name: str = "", **_: Any) -> Optional[dict]:
        """pre_tool_call hook: with the ``toolsets`` filter set, the actual tool boundary of guest turns.

        ``toolsets_for_source`` only shapes the tool list: Hermes still adds other plugins' toolsets
        and default MCP servers to it. Errors block, since a failing hook would let the call through.
        """
        if not in_ghost_session() or tool_name in _SCHEMA_LOOKUP_TOOLS:
            return None
        try:
            from toolsets import resolve_toolset

            names = toolsets_filter(self._ctx.get_config("toolsets", None))
            if not names or any(tool_name in resolve_toolset(name) for name in names):
                return None
        except Exception:
            logger.exception("[tg-ghost] Tool guard failed for %s", tool_name)
        logger.warning("[tg-ghost] Blocked %s in a guest turn", tool_name)
        return {"action": "block", "message": f"Инструмент {tool_name} недоступен в гостевом режиме Telegram."}

    def _spend_budget(self, requested: int) -> int:
        """Take up to ``requested`` messages from the session's history budget; returns the grant."""
        adapter = live_adapter(_session("PROFILE") or None)
        store = adapter.session_store if adapter else None
        limit = int(self._ctx.get_config("max_history_limit", 200) or 200)
        key = _session("KEY")
        fetched = int(store.get_session_metadata(key, HISTORY_COUNTER_KEY, 0) or 0) if store else 0
        granted = max(0, min(requested, limit - fetched))
        if granted == 0:
            raise ub.UserbotError(f"Исчерпан лимит истории на сессию ({limit} сообщений).")
        if store:
            store.set_session_metadata(key, HISTORY_COUNTER_KEY, fetched + granted)
        return granted

    def get_history(self, args: dict, **_: Any) -> str:
        chat_id = _chat_id()
        offset_id = _int_arg(args, "offset_id")
        direction = "after" if str(args.get("direction", "before")).lower() == "after" else "before"
        limit = self._spend_budget(max(1, min(ub.MAX_PAGE, int(args.get("limit") or 10))))
        records = self._userbot.call(ub.fetch_page, chat_id, _session("USER_ID"), offset_id, direction, limit)
        return safety.wrap("ghost_history", tool_result(direction=direction, messages=records))

    def get_messages(self, args: dict, **_: Any) -> str:
        chat_id = _chat_id()
        try:
            ids = [int(i) for i in args.get("message_ids") or []][:MAX_FULL_MESSAGES]
        except (TypeError, ValueError):
            raise ub.UserbotError("message_ids должен быть списком чисел.") from None
        if not ids:
            raise ub.UserbotError("Параметр message_ids обязателен.")
        ids = ids[:self._spend_budget(len(ids))]
        records = self._userbot.call(ub.fetch_messages, chat_id, _session("USER_ID"), ids)
        return safety.wrap("ghost_messages", tool_result(messages=records))

    def media(self, args: dict, **kwargs: Any) -> Any:
        message_id = _int_arg(args, "message_id")
        data, media, sender_id = self._userbot.call(ub.download_media, _chat_id(), message_id)
        from_caller = sender_id == _session("USER_ID")
        if media.is_image:
            question = str(args.get("question") or "Опиши изображение подробно, включая весь видимый текст.")
            # The built-in tool owns vision routing: native image input on capable models, aux model otherwise.
            result = self._ctx.dispatch_tool(
                "vision_analyze", {"image_url": _cache_media(data, media), "question": question},
                task_id=kwargs.get("task_id"))
            return result if from_caller else safety.mark_image(result, message_id)
        if media.has_speech:
            return self._transcribe(message_id, data, media, from_caller)
        return tool_error(f"В сообщении #{message_id} нет изображения или речи ({media.label}).")

    def _transcribe(self, message_id: int, data: bytes, media: ub.MediaInfo, from_caller: bool) -> str:
        from tools.transcription_tools import transcribe_audio

        path = _cache_media(data, media)
        try:
            result = transcribe_audio(path, source="tg-ghost")
        finally:
            with suppress(OSError):
                Path(path).unlink()
        if not result.get("success"):
            return tool_error(result.get("error") or "Не удалось распознать речь.")
        transcript = safety.strip_hidden(result.get("transcript") or "")[0] or "(речь не распознана)"
        data = {"message_id": message_id, "kind": media.kind, "transcript": transcript}
        if from_caller:
            data["from_caller"] = True
        elif flags := safety.injection_flags(transcript):
            data["flags"] = flags
        return safety.wrap("ghost_media", tool_result(data))

    def reset(self, args: dict, **_: Any) -> str:
        """Flag the session for reset. A tool call runs inside the session's own turn, so it
        cannot safely ``/new`` it mid-flight; the flag is consumed before the next message."""
        adapter = live_adapter(_session("PROFILE") or None)
        store = adapter.session_store if adapter else None
        if store is None:
            raise ub.UserbotError("Сброс сейчас недоступен.")
        store.set_session_metadata(_session("KEY"), RESET_REQUESTED_KEY, True)
        return tool_result(status="scheduled", note="Сессия будет сброшена перед следующим сообщением в этом чате.")


HISTORY_SCHEMA = {
    "description": "Fetch older ('before') or newer ('after') messages of the current Telegram chat "
                   "relative to a message id. Messages count against a per-session budget.",
    "parameters": {
        "type": "object",
        "properties": {
            "offset_id": {"type": "integer", "description": "Message id to paginate from."},
            "direction": {"type": "string", "enum": ["before", "after"],
                          "description": "'before' for older messages, 'after' for newer ones."},
            "limit": {"type": "integer", "description": f"Messages to fetch, 1-{ub.MAX_PAGE} (default 10)."},
        },
        "required": ["offset_id"],
    },
}

MESSAGES_SCHEMA = {
    "description": f"Full, untruncated text of specific messages of the current Telegram chat "
                   f"(up to {MAX_FULL_MESSAGES} ids); use it for records flagged 'truncated'.",
    "parameters": {
        "type": "object",
        "properties": {"message_ids": {"type": "array", "items": {"type": "integer"},
                                       "description": "Ids of the messages."}},
        "required": ["message_ids"],
    },
}

MEDIA_SCHEMA = {
    "description": "Understand the media of a message in the current Telegram chat: transcribes speech "
                   "from voice messages, audio, videos and round video notes (кружки); looks at photos "
                   "and images, answering the question about them.",
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": {"type": "integer", "description": "Id of the message with the media."},
            "question": {"type": "string", "description": "What to find out about an image; unused for speech."},
        },
        "required": ["message_id"],
    },
}

RESET_SCHEMA = {
    "description": "Reset this guest session's context on an explicit request to start over or forget "
                   "what was discussed ('забудь', 'начни заново', 'сбрось контекст'). Takes effect "
                   "before the next message in this chat, not this one.",
    "parameters": {"type": "object", "properties": {}},
}
