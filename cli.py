"""``hermes ghost ...``: interactive userbot login and status."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import userbot as ub


def register_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="ghost_command", required=True)
    subs.add_parser("login", help="Authorize the userbot session (phone, code, 2FA)")
    subs.add_parser("status", help="Show whether the userbot session is authorized")


def dispatch(args: argparse.Namespace) -> None:
    creds = ub.load_credentials()
    if creds is None:
        sys.exit(f"Задайте {ub.API_ID_ENV} и {ub.API_HASH_ENV} в $HERMES_HOME/.env")
    if not ub.telethon_available():
        sys.exit("Telethon не установлен: переустановите зависимости плагина")
    try:
        asyncio.run(_login(creds) if args.ghost_command == "login" else _status(creds))
    except KeyboardInterrupt:
        print("\nПрервано.")


async def _login(creds: ub.Credentials) -> None:
    from telethon import TelegramClient

    creds.session_path.parent.mkdir(parents=True, exist_ok=True)
    async with TelegramClient(str(creds.session_path), creds.api_id, creds.api_hash) as client:
        await client.start()
        me = await client.get_me()
    creds.session_file.chmod(0o600)
    print(f"Авторизован как {me.first_name} (@{me.username}, id={me.id}); сессия: {creds.session_file}")


async def _status(creds: ub.Credentials) -> None:
    from telethon import TelegramClient

    if not creds.session_file.exists():
        sys.exit(f"Сессии нет ({creds.session_file}): выполните `hermes ghost login`")
    client = TelegramClient(str(creds.session_path), creds.api_id, creds.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            sys.exit("Сессия не авторизована: выполните `hermes ghost login`")
        me = await client.get_me()
        print(f"Сессия активна: {me.first_name} (@{me.username}, id={me.id})")
    finally:
        await client.disconnect()
