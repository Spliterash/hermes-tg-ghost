"""tg-ghost: Telegram Bot API 10.0 Guest Chat Mode for Hermes Agent.

Guest queries are served by a dedicated ``telegram_ghost`` gateway platform, so sessions,
authorization, the agent run, streaming and delivery all go through the regular gateway
pipeline. The plugin itself only bridges the update in from the core Telegram adapter and
supplies chat context through a Telethon userbot.
"""

from __future__ import annotations

import logging
from typing import Any

from . import cli
from .adapter import MAX_MESSAGE_LENGTH, PLATFORM, PLATFORM_HINT, GhostAdapter, live_adapter
from .ghost_tools import GhostTools
from .userbot import Userbot

logger = logging.getLogger(__name__)

BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


def _bot_token() -> str:
    """The core Telegram bot's token: a scoped secret (``.env``), or, same as the core adapter
    itself reads it, ``platforms.telegram.token`` in config.yaml."""
    from gateway.platforms._shared import get_scoped_secret
    token = str(get_scoped_secret(BOT_TOKEN_ENV, "") or "").strip()
    if token:
        return token
    from hermes_cli.config import load_config
    try:
        cfg = load_config() or {}
    except Exception:
        return ""
    return str(((cfg.get("platforms") or {}).get("telegram") or {}).get("token") or "").strip()


def _ptb_available() -> bool:
    try:
        from telegram import Update
    except ImportError:
        return False
    return hasattr(Update, "guest_message")


def _wire_guest_handler(native: Any, telegram: Any) -> None:
    """Take ``guest_message`` updates off the core Telegram application before its own handlers.

    Group -1 runs first, and stopping the chain keeps core handlers from treating the update as a
    regular message and replying into a chat the bot is not a member of (403).
    """
    from telegram.ext import ApplicationHandlerStop, MessageHandler, filters

    async def on_guest_message(update: Any, context: Any) -> None:
        guest = live_adapter(telegram._owner_transport_profile())
        if guest is None:
            logger.warning("[tg-ghost] Guest query dropped: platform %s is not connected", PLATFORM)
        else:
            try:
                await guest.accept_guest_message(update.guest_message, context.bot, telegram)
            except Exception as exc:
                logger.error("[tg-ghost] Failed to accept guest query: %s", exc, exc_info=True)
        raise ApplicationHandlerStop

    native.add_handler(MessageHandler(filters.UpdateType.GUEST_MESSAGE, on_guest_message), group=-1)


def register(ctx: Any) -> None:
    state = ctx.state
    userbot = Userbot(on_status=lambda status: state.set("userbot", status))
    ctx.on_unload(userbot.shutdown)

    ctx.register_platform(
        name=PLATFORM, label="Telegram Ghost",
        adapter_factory=lambda config: GhostAdapter(
            config, ctx.get_config, userbot, report=lambda status: state.set("platform", status)),
        check_fn=_ptb_available,
        is_connected=lambda _config: bool(_bot_token()),
        env_enablement_fn=lambda: {"ghost": True} if _bot_token() else None,
        required_env=[BOT_TOKEN_ENV],
        install_hint="python-telegram-bot>=22.8 (Bot API 10.0) is required",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="👻",
        allow_update_command=False,
        platform_hint=PLATFORM_HINT,
    )
    ctx.register_telegram_handler(_wire_guest_handler)
    GhostTools(ctx, userbot).register()
    ctx.register_cli_command(
        name="ghost", help="Telegram Ghost: userbot login and status",
        setup_fn=cli.register_cli, handler_fn=cli.dispatch)
