"""Telegram Ghost dashboard backend, mounted at /api/plugins/tg-ghost/.

Loaded standalone by the dashboard server (not as part of the plugin package), and runs in a
different process than the gateway: it reads what the gateway recorded in plugin state. The only
Telethon client it opens is the login one, on a separate session file that replaces the userbot
session once authorized. Settings are edited through the dashboard's generic plugin settings form
(``config_schema`` in plugin.yaml).
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

PLUGIN_ID = "tg-ghost"
PLATFORM = "telegram_ghost"
API_ID_ENV = "TELEGRAM_USERBOT_API_ID"
API_HASH_ENV = "TELEGRAM_USERBOT_API_HASH"

router = APIRouter()

# The login in progress: its client, phone and code hash. One owner, one login at a time.
_login: Dict[str, Any] = {}


def _auth_dir() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "auth"


def _credentials() -> Optional[tuple[int, str]]:
    from hermes_cli.config import get_env_value

    api_id = str(get_env_value(API_ID_ENV) or "").strip()
    api_hash = str(get_env_value(API_HASH_ENV) or "").strip()
    return (int(api_id), api_hash) if api_id.isdigit() and api_hash else None


def _telethon_available() -> bool:
    try:
        import telethon  # noqa: F401
    except ImportError:
        return False
    return True


def _state() -> Any:
    from hermes_cli.plugins_state import PluginState
    return PluginState(PLUGIN_ID)


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
    state = _state()
    return {
        "at": int(time.time()),
        "platform": state.get("platform"),
        "userbot": {
            "telethon": _telethon_available(),
            "configured": _credentials() is not None,
            "session_exists": (_auth_dir() / "userbot.session").exists(),
            "last_status": state.get("userbot"),
            "login_step": _login.get("step"),
        },
        "sessions": _sessions(),
    }


class LoginStart(BaseModel):
    phone: str


class LoginCode(BaseModel):
    code: str


class LoginPassword(BaseModel):
    password: str


async def _drop_login() -> None:
    client = _login.get("client")
    _login.clear()
    if client is not None:
        with contextlib.suppress(Exception):
            await client.disconnect()
    for suffix in (".session", ".session-journal"):
        with contextlib.suppress(OSError):
            (_auth_dir() / f"userbot-login{suffix}").unlink()


def _client() -> Any:
    client = _login.get("client")
    if client is None:
        raise HTTPException(status_code=409, detail="Вход не начат: запросите код заново.")
    return client


def _fail(exc: Exception) -> HTTPException:
    from telethon import errors

    messages = {
        errors.PhoneNumberInvalidError: "Неверный номер телефона.",
        errors.PhoneCodeInvalidError: "Неверный код.",
        errors.PhoneCodeExpiredError: "Код истёк: запросите новый.",
        errors.PasswordHashInvalidError: "Неверный пароль 2FA.",
    }
    for kind, message in messages.items():
        if isinstance(exc, kind):
            return HTTPException(status_code=400, detail=message)
    if isinstance(exc, errors.FloodWaitError):
        return HTTPException(status_code=429, detail=f"Telegram просит подождать {exc.seconds} с.")
    return HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")


async def _finish() -> Dict[str, Any]:
    """Promote the login session to the userbot session the gateway reads."""
    client = _client()
    me = await client.get_me()
    await client.disconnect()
    _login.clear()
    auth = _auth_dir()
    session = auth / "userbot.session"
    os.replace(auth / "userbot-login.session", session)
    session.chmod(0o600)
    user = f"@{me.username}" if me.username else str(me.id)
    _state().set("userbot", {"state": "authorized", "user": user, "at": int(time.time())})
    return {"step": "done", "user": user}


@router.post("/login/start")
async def login_start(body: LoginStart) -> Dict[str, Any]:
    if not _telethon_available():
        raise HTTPException(status_code=400, detail="Telethon не установлен: переустановите зависимости плагина.")
    creds = _credentials()
    if creds is None:
        raise HTTPException(status_code=400, detail=f"Сначала задайте {API_ID_ENV} и {API_HASH_ENV} в настройках плагина.")
    from telethon import TelegramClient

    await _drop_login()
    _auth_dir().mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(_auth_dir() / "userbot-login"), *creds)
    try:
        await client.connect()
        sent = await client.send_code_request(body.phone.strip())
    except Exception as exc:
        await client.disconnect()
        raise _fail(exc) from None
    _login.update(client=client, phone=body.phone.strip(), code_hash=sent.phone_code_hash, step="code")
    return {"step": "code"}


@router.post("/login/code")
async def login_code(body: LoginCode) -> Dict[str, Any]:
    from telethon.errors import SessionPasswordNeededError

    client = _client()
    try:
        await client.sign_in(_login["phone"], body.code.strip(), phone_code_hash=_login["code_hash"])
    except SessionPasswordNeededError:
        _login["step"] = "password"
        return {"step": "password"}
    except Exception as exc:
        raise _fail(exc) from None
    return await _finish()


@router.post("/login/password")
async def login_password(body: LoginPassword) -> Dict[str, Any]:
    client = _client()
    try:
        await client.sign_in(password=body.password)
    except Exception as exc:
        raise _fail(exc) from None
    return await _finish()


@router.post("/login/cancel")
async def login_cancel() -> Dict[str, Any]:
    await _drop_login()
    return {"step": None}
