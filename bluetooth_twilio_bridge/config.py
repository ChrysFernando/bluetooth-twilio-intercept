"""
config.py — Load, save, and encrypt the application configuration.

Credentials are encrypted with Fernet (symmetric key derived from a
machine-specific secret so the file is not plain-text but also does not
require the user to remember a separate password).
"""

from __future__ import annotations

import base64
import json
import os
import socket
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Config lives in the same directory as this module
CONFIG_PATH = Path(__file__).parent / "config.json"

# Sensitive fields that will be stored encrypted
_SENSITIVE = {"account_sid", "auth_token"}

# Salt is stable (stored in config), generated on first run
_SALT_FIELD = "_salt"

# PBKDF2 iteration count — see OWASP recommendations for PBKDF2-HMAC-SHA256
_PBKDF2_ITERATIONS = 480_000


def _machine_secret() -> bytes:
    """Derive a stable, machine-specific secret."""
    hostname = socket.gethostname().encode()
    username = os.environ.get("USERNAME", os.environ.get("USER", "user")).encode()
    return hostname + b":" + username


def _build_fernet(salt: bytes) -> Fernet:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    key = base64.urlsafe_b64encode(kdf.derive(_machine_secret()))
    return Fernet(key)


def _new_salt() -> str:
    return base64.b64encode(os.urandom(16)).decode()


def encrypt_value(value: str, fernet: Fernet) -> str:
    return fernet.encrypt(value.encode()).decode()


def decrypt_value(token: str, fernet: Fernet) -> str:
    return fernet.decrypt(token.encode()).decode()


def load_config() -> dict[str, Any]:
    """Load and decrypt config from disk. Returns {} if not found."""
    if not CONFIG_PATH.exists():
        return {}

    with CONFIG_PATH.open() as fh:
        raw: dict[str, Any] = json.load(fh)

    salt = base64.b64decode(raw.get(_SALT_FIELD, ""))
    if not salt:
        return raw  # Old/plain format — just return as-is

    fernet = _build_fernet(salt)
    result: dict[str, Any] = {}
    for k, v in raw.items():
        if k == _SALT_FIELD:
            continue
        if k in _SENSITIVE and isinstance(v, str):
            try:
                result[k] = decrypt_value(v, fernet)
            except Exception:
                result[k] = v  # Fallback: return raw if decryption fails
        else:
            result[k] = v
    return result


def save_config(cfg: dict[str, Any]) -> None:
    """Encrypt sensitive fields and write config to disk."""
    salt_b64: str = cfg.get(_SALT_FIELD) or _new_salt()
    salt = base64.b64decode(salt_b64)
    fernet = _build_fernet(salt)

    serializable: dict[str, Any] = {_SALT_FIELD: salt_b64}
    for k, v in cfg.items():
        if k == _SALT_FIELD:
            continue
        if k in _SENSITIVE and isinstance(v, str):
            serializable[k] = encrypt_value(v, fernet)
        else:
            serializable[k] = v

    CONFIG_PATH.write_text(json.dumps(serializable, indent=2))


def prompt_credentials(existing: dict[str, Any]) -> dict[str, Any]:
    """
    Interactively prompt the user for missing/re-entered Twilio credentials.
    Returns an updated copy of *existing*.
    """
    import getpass

    cfg = dict(existing)
    print("\nEnter Twilio credentials (press Enter to keep existing value):")

    sid = input(
        f"  Twilio Account SID [{cfg.get('account_sid', '')}]: "
    ).strip()
    if sid:
        cfg["account_sid"] = sid

    token = getpass.getpass(
        f"  Twilio Auth Token [{'*' * 8 if cfg.get('auth_token') else ''}]: "
    ).strip()
    if token:
        cfg["auth_token"] = token

    from_number = input(
        f"  Twilio FROM number (your Twilio phone) [{cfg.get('from_number', '')}]: "
    ).strip()
    if from_number:
        cfg["from_number"] = from_number

    return cfg
