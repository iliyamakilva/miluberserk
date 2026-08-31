"""Small shared utilities with no dependency on the bot dispatcher."""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime

import qrcode


_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_TO_ASCII_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def make_qr(link: str, user_id) -> str:
    fd, path = tempfile.mkstemp(prefix=f"sub_{user_id}_", suffix=".png")
    os.close(fd)
    image = qrcode.make(link)
    image.save(path)
    return path


def cleanup_qr(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def to_persian_digits(value) -> str:
    return str(value).translate(_PERSIAN_DIGITS)


def normalize_digits(value) -> str:
    """Convert Persian/Arabic digits to ASCII and trim surrounding whitespace."""
    return str(value or "").translate(_TO_ASCII_DIGITS).strip()


def parse_int(value, *, allow_negative: bool = False, default=None):
    """Parse a human-entered integer with commas and Persian/Arabic digits."""
    raw = normalize_digits(value).replace(",", "").replace("٬", "").replace(" ", "")
    if allow_negative and raw.startswith("-"):
        digits = raw[1:]
        if digits.isdigit():
            return -int(digits)
    elif raw.isdigit():
        return int(raw)
    return default


def gregorian_to_jalali(gy: int, gm: int, gd: int):
    """Convert a Gregorian date to Jalali without an external dependency."""
    if not (1 <= gm <= 12 and 1 <= gd <= 31):
        raise ValueError("invalid Gregorian date")

    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        - 80
        + gd
        + g_d_m[gm - 1]
    )
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def _parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)

    raw = str(value).strip()
    if not raw or raw == "-":
        return None

    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    )
    for fmt in formats:
        candidate = raw[:19] if "%S" in fmt else raw[:10]
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def jalali_date(value, persian_digits: bool = True) -> str:
    dt = _parse_datetime(value)
    if not dt:
        return "-"
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    output = f"{jy:04d}/{jm:02d}/{jd:02d}"
    return to_persian_digits(output) if persian_digits else output


def format_dual_datetime(value, show_time: bool = True) -> str:
    """Return Gregorian and Jalali representations with a consistent format."""
    dt = _parse_datetime(value)
    if not dt:
        return "-"
    gregorian = dt.strftime("%Y-%m-%d %H:%M") if show_time else dt.strftime("%Y-%m-%d")
    return f"{gregorian} | شمسی: {jalali_date(dt)}"
