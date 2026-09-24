"""Telegram Ghost dashboard backend, mounted at /api/plugins/tg-ghost/.

Loaded standalone by the dashboard server (not as part of the plugin package), and runs in a
different process than the gateway: it never opens the Telethon session, it only reads what the
gateway recorded in plugin state. Settings are edited through the dashboard's generic plugin
settings form (``config_schema`` in plugin.yaml).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from fastapi import APIRouter

PLUGIN_ID = "tg-ghost"
PLATFORM = "telegram_ghost"

router = APIRouter()


def _userbot() -> Dict[str, Any]:
    from gateway.platforms._shared import get_scoped_secret
    from hermes_cli.plugins_state import PluginState
    from hermes_constants import get_hermes_home

    return {
        "configured": bool(get_scoped_secret("TELEGRAM_USERBOT_API_ID", "")
                           and get_scoped_secret("TELEGRAM_USERBOT_API_HASH", "")),
        "session_exists": (get_hermes_home() / "auth" / "userbot.session").exists(),
        "last_status": PluginState(PLUGIN_ID).get("userbot"),
    }


def _sessions() -> List[Dict[str, Any]]:
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        rows = db.list_sessions_rich(source=PLATFORM, limit=50, order_by_last_active=True)
    finally:
        release_or_close(db)
    return [
        {key: row.get(key) for key in ("id", "title", "session_key", "message_count", "last_active", "ended_at")}
        for row in rows
    ]


@router.get("/status")
async def status() -> Dict[str, Any]:
    return {"at": int(time.time()), "userbot": _userbot(), "sessions": _sessions()}
