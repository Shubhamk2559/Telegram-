"""QR-code login (Telethon) + secure session storage.

Kabhi print nahi hota: session content, password, API hash, login token URL.
"""
from __future__ import annotations

import asyncio
import contextlib
import getpass
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError

from .config import Settings

QR_WAIT_SECONDS = 25
MAX_QR_REFRESHES = 12  # ~5 minute tak try, phir ruk jaata hai


class AuthError(Exception):
    pass


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass  # Windows par chmod limited hai


def secure_session_files(session_base: Path) -> None:
    _chmod(session_base.parent, 0o700)
    for suffix in (".session", ".session-journal"):
        p = session_base.with_name(session_base.name + suffix)
        if p.exists():
            _chmod(p, 0o600)


def _print_qr(url: str) -> None:
    try:
        import qrcode
    except ImportError as e:
        raise AuthError("'qrcode' package missing. Chalao: pip install -r requirements.txt") from e
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


async def _two_step(client: TelegramClient) -> None:
    for _ in range(3):
        password = getpass.getpass("Telegram 2-step password (type karoge to dikhega nahi): ")
        try:
            await client.sign_in(password=password)
            return
        except PasswordHashInvalidError:
            print("Password galat hai, dobara try karo.")
        finally:
            password = None  # noqa: F841
    raise AuthError("2-step password 3 baar galat. Baad me dobara try karo.")


async def _qr_login(client: TelegramClient) -> None:
    qr = await client.qr_login()
    for _ in range(MAX_QR_REFRESHES):
        print("\nTelegram app kholo -> Settings -> Devices -> 'Link Desktop Device' -> is QR ko scan karo:\n")
        _print_qr(qr.url)
        print(f"(QR {QR_WAIT_SECONDS} sec me expire hoga, apne aap naya aa jayega)")
        try:
            await qr.wait(timeout=QR_WAIT_SECONDS)
            return
        except asyncio.TimeoutError:
            await qr.recreate()
        except SessionPasswordNeededError:
            print("\nQR scan ho gaya. Ab 2-step verification password chahiye.")
            await _two_step(client)
            return
    raise AuthError("QR scan nahi hua (time out). Dobara chalao.")


@contextlib.asynccontextmanager
async def telegram_session(settings: Settings):
    """Connected + authorized client deta hai; band hone par disconnect."""
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    secure_session_files(settings.session_base)
    client = TelegramClient(str(settings.session_base), settings.api_id, settings.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("Saved session nahi mila / expire ho gaya. QR login shuru...")
            await _qr_login(client)
        secure_session_files(settings.session_base)
        yield client
    finally:
        secure_session_files(settings.session_base)
        await client.disconnect()
