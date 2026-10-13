"""
Credential encryption helpers.

Notification credentials (SMTP passwords, API keys, webhook URLs) are stored in
the SQLite database.  They are encrypted at rest with a Fernet key that lives in
a separate key file so the database alone is not enough to read them.

The key file is created on first use and must be kept alongside - but not
inside - the database.  If the key is lost, saved credentials cannot be
recovered and have to be re-entered.
"""
import logging
import os
import stat
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

DEFAULT_KEY_PATH = Path(__file__).resolve().parent.parent / '.secret.key'

_cipher: Optional[Fernet] = None
_cipher_path: Optional[Path] = None


def _load_or_create_key(key_path: Path) -> bytes:
    """Read the Fernet key, generating one on first run."""
    if key_path.exists():
        key = key_path.read_bytes().strip()
        if key:
            return key
        logger.warning(f"Key file {key_path} was empty - generating a new key")

    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)

    # Restrict to the owner where the platform supports it
    try:
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as e:  # pragma: no cover - platform dependent
        logger.debug(f"Could not tighten permissions on {key_path}: {e}")

    logger.info(f"Created credential encryption key: {key_path}")
    return key


def get_cipher(key_path: Optional[Path] = None) -> Fernet:
    """Return the process-wide Fernet cipher, creating the key if needed."""
    global _cipher, _cipher_path

    path = Path(key_path) if key_path else DEFAULT_KEY_PATH
    if _cipher is None or _cipher_path != path:
        _cipher = Fernet(_load_or_create_key(path))
        _cipher_path = path
    return _cipher


def encrypt_str(plaintext: str) -> str:
    """Encrypt a string for storage. Returns a token string."""
    if plaintext is None:
        return None
    return get_cipher().encrypt(plaintext.encode('utf-8')).decode('utf-8')


def decrypt_str(token: str) -> str:
    """
    Decrypt a stored string.

    Values written before encryption was introduced are plain text; those are
    returned unchanged so existing databases keep working.
    """
    if not token:
        return token
    try:
        return get_cipher().decrypt(token.encode('utf-8')).decode('utf-8')
    except InvalidToken:
        # Legacy plaintext value (or a value written with a different key)
        return token
    except Exception as e:
        logger.error(f"Could not decrypt stored value: {e}")
        return token
