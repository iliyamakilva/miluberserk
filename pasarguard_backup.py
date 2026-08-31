"""Automated PasarGuard panel backups (DB dump + config files -> Telegram).

Unlike backup.py (which backs up the BOT's own SQLite database), this module
backs up the *PasarGuard panel* itself: its database and config/cert files.

Two execution modes, controlled by PASARGUARD_BACKUP_MODE:
  - "local": the bot process runs on the same machine as the panel, so the
    backup command is just a local subprocess. This is today's setup.
  - "ssh": panel lives on a different server than the bot. Requires the
    optional `asyncssh` dependency (only imported when this mode is used,
    so nothing breaks for people who never touch this mode).

The actual dump/tar command is fully admin-defined (PASARGUARD_BACKUP_COMMAND)
since only the admin knows their container names and compose paths — this
module does not guess at those to avoid running something wrong against
real production data. The command's contract: it must print the absolute
path of the resulting backup file as the last line of stdout.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram.types import InputFile

import db
from config import (
    PASARGUARD_BACKUP_COMMAND,
    PASARGUARD_BACKUP_DIR,
    PASARGUARD_BACKUP_ENABLED,
    PASARGUARD_BACKUP_INTERVAL_SECONDS,
    PASARGUARD_BACKUP_MODE,
    PASARGUARD_BACKUP_RETENTION_COUNT,
    PASARGUARD_BACKUP_SSH_HOST,
    PASARGUARD_BACKUP_SSH_KEY_PATH,
    PASARGUARD_BACKUP_SSH_PORT,
    PASARGUARD_BACKUP_SSH_USER,
    pasarguard_backup_configured,
)

logger = logging.getLogger(__name__)

OPERATION_TYPE = "pasarguard_backup"
COMMAND_TIMEOUT_SECONDS = 900  # 15 minutes; a big TimescaleDB dump can be slow


class PasarGuardBackupError(RuntimeError):
    pass


async def _run_local(command: str) -> str:
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=COMMAND_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise PasarGuardBackupError("اجرای دستور بکاپ بیش از حد طول کشید (timeout).") from exc
    if proc.returncode != 0:
        raise PasarGuardBackupError(f"دستور بکاپ با خطا تمام شد:\n{stderr.decode(errors='ignore')[-800:]}")
    lines = [line for line in stdout.decode(errors="ignore").splitlines() if line.strip()]
    if not lines:
        raise PasarGuardBackupError("دستور بکاپ هیچ مسیر فایلی چاپ نکرد.")
    return lines[-1].strip()


async def _run_ssh(command: str) -> str:
    try:
        import asyncssh  # noqa: F401  (optional dependency, only needed in ssh mode)
    except ImportError as exc:
        raise PasarGuardBackupError(
            "حالت ssh فعال است ولی پکیج asyncssh نصب نیست. `pip install asyncssh` را به requirements.txt اضافه کن."
        ) from exc

    local_download_dir = Path(PASARGUARD_BACKUP_DIR)
    local_download_dir.mkdir(parents=True, exist_ok=True)

    try:
        async with asyncssh.connect(
            PASARGUARD_BACKUP_SSH_HOST,
            port=PASARGUARD_BACKUP_SSH_PORT,
            username=PASARGUARD_BACKUP_SSH_USER,
            client_keys=[PASARGUARD_BACKUP_SSH_KEY_PATH],
            known_hosts=None,
        ) as conn:
            result = await asyncio.wait_for(
                conn.run(command, check=False), timeout=COMMAND_TIMEOUT_SECONDS
            )
            if result.exit_status != 0:
                raise PasarGuardBackupError(f"دستور بکاپ (ssh) با خطا تمام شد:\n{str(result.stderr)[-800:]}")
            lines = [line for line in str(result.stdout).splitlines() if line.strip()]
            if not lines:
                raise PasarGuardBackupError("دستور بکاپ (ssh) هیچ مسیر فایلی چاپ نکرد.")
            remote_path = lines[-1].strip()
            local_path = local_download_dir / Path(remote_path).name
            async with conn.start_sftp_client() as sftp:
                await sftp.get(remote_path, str(local_path))
            return str(local_path)
    except asyncio.TimeoutError as exc:
        raise PasarGuardBackupError("اتصال یا اجرای دستور روی پنل بیش از حد طول کشید (timeout).") from exc
    except OSError as exc:
        raise PasarGuardBackupError(f"اتصال SSH به پنل برقرار نشد: {exc}") from exc


async def _run_backup_command() -> str:
    if not PASARGUARD_BACKUP_COMMAND:
        raise PasarGuardBackupError("PASARGUARD_BACKUP_COMMAND تنظیم نشده است.")
    if PASARGUARD_BACKUP_MODE == "ssh":
        return await _run_ssh(PASARGUARD_BACKUP_COMMAND)
    return await _run_local(PASARGUARD_BACKUP_COMMAND)


def _enforce_local_retention(latest_file: str) -> None:
    """Keep only the newest PASARGUARD_BACKUP_RETENTION_COUNT files that look
    like backups in PASARGUARD_BACKUP_DIR, delete the rest."""
    directory = Path(latest_file).parent
    if not directory.is_dir():
        return
    files = sorted(
        (p for p in directory.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in files[PASARGUARD_BACKUP_RETENTION_COUNT:]:
        try:
            stale.unlink()
        except OSError as exc:
            logger.warning("pasarguard_backup: failed to remove old backup %s: %s", stale, exc)


async def run_backup_and_send(bot, admin_ids, triggered_by=None) -> tuple[bool, str]:
    """Run the configured backup command and send the result to admins.

    Returns (ok, message) — message is a short human-readable summary
    suitable for showing directly in the admin panel.
    """
    if not PASARGUARD_BACKUP_ENABLED:
        return False, "بکاپ خودکار پاسارگارد خاموش است (PASARGUARD_BACKUP_ENABLED)."
    if not pasarguard_backup_configured():
        return False, "بکاپ پاسارگارد کامل تنظیم نشده است (دستور بکاپ یا اطلاعات SSH ناقص است)."

    try:
        file_path = await _run_backup_command()
        if not os.path.exists(file_path):
            raise PasarGuardBackupError(f"فایل بکاپ در مسیر گزارش‌شده پیدا نشد: {file_path}")
        file_size = os.path.getsize(file_path)
        if file_size <= 0:
            raise PasarGuardBackupError("فایل بکاپ خالی است.")
    except PasarGuardBackupError as exc:
        db.log_backup_operation(triggered_by, OPERATION_TYPE, None, 0, status="failed", note=str(exc))
        for admin_id in admin_ids:
            try:
                await bot.send_message(admin_id, f"❌ بکاپ پاسارگارد ناموفق بود:\n{exc}")
            except Exception:
                logger.exception("pasarguard_backup: failed to notify admin %s", admin_id)
        return False, str(exc)

    if PASARGUARD_BACKUP_MODE != "ssh":
        _enforce_local_retention(file_path)

    file_name = os.path.basename(file_path)
    size_mb = file_size / (1024 * 1024)
    caption = f"🔐 بکاپ پنل PasarGuard\n{file_name}\nحجم: {size_mb:.1f}MB"
    sent_to_anyone = False
    for admin_id in admin_ids:
        try:
            await bot.send_document(admin_id, InputFile(file_path), caption=caption)
            sent_to_anyone = True
        except Exception:
            logger.exception("pasarguard_backup: failed to send backup to admin %s", admin_id)

    status = "ok" if sent_to_anyone else "failed"
    note = "" if sent_to_anyone else "فایل ساخته شد ولی ارسال به هیچ ادمینی موفق نبود."
    db.log_backup_operation(triggered_by, OPERATION_TYPE, file_name, file_size, status=status, note=note)

    if not sent_to_anyone:
        return False, f"فایل بکاپ ساخته شد ({file_name}, {size_mb:.1f}MB) ولی ارسال به تلگرام ناموفق بود."
    return True, f"✅ بکاپ پاسارگارد ({file_name}, {size_mb:.1f}MB) ساخته و ارسال شد."


async def pasarguard_backup_loop(bot, admin_ids, interval_seconds=PASARGUARD_BACKUP_INTERVAL_SECONDS):
    """Background task: run the PasarGuard backup on a fixed interval.

    Mirrors backup.daily_backup_loop's shape so both loops behave the same
    way operationally (never dies on a single failed run, just logs and
    waits for the next cycle).
    """
    if not PASARGUARD_BACKUP_ENABLED:
        return
    while True:
        try:
            await run_backup_and_send(bot, admin_ids, triggered_by=None)
        except Exception:
            logger.exception("pasarguard_backup_loop: unexpected error during scheduled run")
        await asyncio.sleep(max(3600, int(interval_seconds or 0)))
