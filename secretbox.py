"""Encryption at rest for the stored Pocket Option session (SSID).

Key: env SSID_KEY (a Fernet key) if set, otherwise a key file next to the database (created once, mode 0600).
Setting SSID_KEY in the Railway variables keeps the key off the volume, which is the stronger setup.
"""
import os
import sqlite3
from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"
_fernet = None


def _key_path(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "ssid.key")


def init(db_path: str):
    global _fernet
    key = os.getenv("SSID_KEY", "").strip().encode()
    if not key:
        path = _key_path(db_path)
        if os.path.exists(path):
            key = open(path, "rb").read().strip()
        else:
            key = Fernet.generate_key()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
    _fernet = Fernet(key)


def encrypt(value: str) -> str:
    if not value or value.startswith(PREFIX):
        return value
    return PREFIX + _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """Legacy plaintext values are returned as is. An undecryptable value (wrong key) reads as empty, so the user reconnects."""
    if not value or not value.startswith(PREFIX):
        return value or ""
    try:
        return _fernet.decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        return ""


def migrate(db_path: str) -> int:
    """Encrypt any plaintext SSID already in the database."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT uid, po_ssid FROM users WHERE po_ssid != '' AND po_ssid NOT LIKE ?", (PREFIX + "%",)).fetchall()
    for uid, v in rows:
        conn.execute("UPDATE users SET po_ssid=? WHERE uid=?", (encrypt(v), uid))
    conn.commit()
    conn.close()
    return len(rows)
