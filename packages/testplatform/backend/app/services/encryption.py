"""
Encryption service for secure storage of sensitive data like API keys.

Uses Fernet symmetric encryption from the cryptography library.
The encryption key is derived from an environment variable or generated on first use.
"""

import os
import base64
import hashlib
import logging
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# Try to import cryptography, fall back to simple obfuscation if not available
try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    logger.warning("cryptography library not available - using basic obfuscation")


class EncryptionService:
    """Service for encrypting and decrypting sensitive data."""

    # Key file location (in production, use secure key management)
    KEY_FILE = Path("backend/.encryption_key")
    SALT_FILE = Path("backend/.encryption_salt")

    def __init__(self):
        """Initialize encryption service."""
        self._fernet: Optional[object] = None
        self._initialize_encryption()

    def _initialize_encryption(self):
        """Initialize the encryption key."""
        if not CRYPTO_AVAILABLE:
            logger.warning("Encryption not available - data will be obfuscated only")
            return

        # Try to get key from environment
        master_key = os.environ.get("ENCRYPTION_KEY")

        if master_key:
            # Derive key from master key
            self._fernet = self._create_fernet_from_password(master_key)
        else:
            # Load or generate key file
            self._fernet = self._load_or_generate_key()

    def _create_fernet_from_password(self, password: str) -> object:
        """Create Fernet instance from password."""
        # Get or create salt
        if self.SALT_FILE.exists():
            salt = self.SALT_FILE.read_bytes()
        else:
            salt = os.urandom(16)
            self.SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.SALT_FILE.write_bytes(salt)

        # Derive key using PBKDF2
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        return Fernet(key)

    def _load_or_generate_key(self) -> object:
        """Load existing key or generate new one."""
        if self.KEY_FILE.exists():
            try:
                key = self.KEY_FILE.read_bytes()
                return Fernet(key)
            except Exception as e:
                logger.error(f"Failed to load encryption key: {e}")

        # Generate new key
        key = Fernet.generate_key()
        self.KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.KEY_FILE.write_bytes(key)
        # Restrict file permissions (Unix only)
        try:
            os.chmod(self.KEY_FILE, 0o600)
        except Exception:
            pass
        logger.info("Generated new encryption key")
        return Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt a string.

        Args:
            plaintext: String to encrypt

        Returns:
            Encrypted string (base64 encoded)
        """
        if not CRYPTO_AVAILABLE or not self._fernet:
            # Fallback to basic obfuscation
            return self._obfuscate(plaintext)

        try:
            encrypted = self._fernet.encrypt(plaintext.encode())
            return encrypted.decode('utf-8')
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            return self._obfuscate(plaintext)

    def decrypt(self, ciphertext: str) -> str:
        """
        Decrypt an encrypted string.

        Args:
            ciphertext: Encrypted string (base64 encoded)

        Returns:
            Decrypted plaintext string
        """
        if not CRYPTO_AVAILABLE or not self._fernet:
            # Fallback to basic de-obfuscation
            return self._deobfuscate(ciphertext)

        try:
            decrypted = self._fernet.decrypt(ciphertext.encode())
            return decrypted.decode('utf-8')
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            # Try de-obfuscation in case it was stored with fallback
            return self._deobfuscate(ciphertext)

    def _obfuscate(self, text: str) -> str:
        """Simple obfuscation fallback (NOT secure)."""
        # Base64 encode with a simple XOR
        xor_key = 0x5A
        obfuscated = bytes([ord(c) ^ xor_key for c in text])
        return "OBF:" + base64.b64encode(obfuscated).decode('utf-8')

    def _deobfuscate(self, text: str) -> str:
        """Reverse simple obfuscation."""
        if text.startswith("OBF:"):
            text = text[4:]
            try:
                decoded = base64.b64decode(text)
                xor_key = 0x5A
                return ''.join([chr(b ^ xor_key) for b in decoded])
            except Exception:
                pass
        return text

    def is_encrypted(self, text: str) -> bool:
        """Check if text appears to be encrypted."""
        if text.startswith("OBF:"):
            return True
        # Fernet tokens start with 'gAAA'
        if text.startswith("gAAA"):
            return True
        return False


# Global encryption service instance
_encryption_service: Optional[EncryptionService] = None


def get_encryption_service() -> EncryptionService:
    """Get the global encryption service instance."""
    global _encryption_service
    if _encryption_service is None:
        _encryption_service = EncryptionService()
    return _encryption_service


def encrypt_api_key(api_key: str) -> str:
    """Encrypt an API key for storage."""
    return get_encryption_service().encrypt(api_key)


def decrypt_api_key(encrypted_key: str) -> str:
    """Decrypt a stored API key."""
    return get_encryption_service().decrypt(encrypted_key)


def is_key_encrypted(key: str) -> bool:
    """Check if an API key is already encrypted."""
    return get_encryption_service().is_encrypted(key)
