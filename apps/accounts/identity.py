"""Registration-number HMAC and card-name encryption.

Pure functions over settings; no DB access. Services call these and persist the results.

Registration numbers are never stored: only ``HMAC-SHA256(REG_NO_HMAC_KEY, normalized)`` (for the
unique Sybil-resistance binding) and the last four digits (for a moderator to eyeball).

Card names are stored as Fernet ciphertext so a leaked ``pg_dump`` doesn't reveal them.
"""
import hashlib
import hmac
import re

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

MAX_CARD_NAME_LENGTH = 100


class InvalidRegNo(ValueError):
    """The registration number doesn't match ``REG_NO_PATTERN`` after normalization."""

    messages = ["Enter a valid registration number."]


class InvalidCardName(ValueError):
    messages = ["Enter the name exactly as printed on the ID card."]


class CardNameUnreadable(Exception):
    """Ciphertext can't be decrypted with any configured key (a key was dropped or corrupted)."""


def normalize_reg_no(raw: str) -> str:
    """Strip whitespace and dashes, then require a full match of ``REG_NO_PATTERN``.

    The pattern is compiled with ``re.ASCII``: without it ``\\d`` matches Arabic-Indic and other
    Unicode digits, so two visually different strings could normalize to different HMACs for the
    same card, or the same HMAC could be reached by lookalikes. Always a string, never an int
    (leading zeros are significant).
    """
    if not isinstance(raw, str):
        raise InvalidRegNo()
    cleaned = re.sub(r"[\s\-]", "", raw)
    if not re.fullmatch(settings.REG_NO_PATTERN, cleaned, flags=re.ASCII):
        raise InvalidRegNo()
    return cleaned


def redact_reg_nos(text: str) -> str:
    """Blank out anything in free text that could be a registration number.

    Deliberately broad: any run of six or more digits, however separated, plus any word matching
    ``REG_NO_PATTERN``. A redacted date or ticket number costs little; a stored reg no breaks the
    promise that we keep only its hash.
    """
    text = re.sub(r"\d(?:[\s\-./_]*\d){5,}", "[number removed]", text)
    return re.sub(
        r"\w+",
        lambda m: "[number removed]" if re.fullmatch(settings.REG_NO_PATTERN, m.group(), flags=re.ASCII) else m.group(),
        text,
    )


def _hmac_key() -> bytes:
    key = settings.REG_NO_HMAC_KEY
    if not key:
        raise ImproperlyConfigured("REG_NO_HMAC_KEY is not set.")
    return key.encode()


def hash_reg_no(raw: str) -> tuple[str, str]:
    """Return ``(reg_no_hmac, reg_no_last4)`` for a raw registration number."""
    normalized = normalize_reg_no(raw)
    digest = hmac.new(_hmac_key(), normalized.encode(), hashlib.sha256).hexdigest()
    return digest, normalized[-4:]


def _fernet() -> MultiFernet:
    keys = settings.CARD_NAME_KEYS
    if not keys:
        raise ImproperlyConfigured("CARD_NAME_KEYS is not set.")
    # MultiFernet encrypts with the first key and tries every key to decrypt (rotation).
    return MultiFernet([Fernet(k) for k in keys])


def normalize_card_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not name or len(name) > MAX_CARD_NAME_LENGTH:
        raise InvalidCardName()
    return name


def encrypt_card_name(raw: str) -> str:
    return _fernet().encrypt(normalize_card_name(raw).encode()).decode()


def decrypt_card_name(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise CardNameUnreadable() from exc
