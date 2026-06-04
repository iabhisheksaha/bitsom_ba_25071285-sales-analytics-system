"""
Agent 4: Credential Management
Stores job-site passwords in a Fernet-encrypted file.
Access is granted only when the correct key is supplied at runtime.
"""

import json
import os
import stat
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken


class CredentialManager:
    """
    Maintains an encrypted credential store.
    The encryption key is never persisted alongside the credentials file.
    """

    def __init__(self, credentials_path: str):
        self.credentials_path = Path(credentials_path)

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def generate_key() -> bytes:
        """Generate a new Fernet key. Call once; store the output securely."""
        return Fernet.generate_key()

    def _fernet(self, key: bytes) -> Fernet:
        return Fernet(key)

    # ------------------------------------------------------------------
    # Initialise a new store
    # ------------------------------------------------------------------

    def init_store(self, key: bytes, credentials: dict) -> None:
        """
        Create or overwrite the encrypted credentials file.

        credentials format::

            {
                "linkedin": {"username": "...", "password": "..."},
                "naukri":   {"username": "...", "password": "..."},
                "indeed":   {"username": "...", "password": "..."}
            }
        """
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(credentials).encode()
        encrypted = self._fernet(key).encrypt(payload)
        self.credentials_path.write_bytes(encrypted)
        # Restrict file to owner read/write only (chmod 600)
        self.credentials_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        print(f"[Agent4] Credential store initialised at {self.credentials_path}")

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _load_all(self, key: bytes) -> dict:
        if not self.credentials_path.exists():
            raise FileNotFoundError(
                f"Credential store not found: {self.credentials_path}. "
                "Run init_store() first."
            )
        try:
            encrypted = self.credentials_path.read_bytes()
            payload = self._fernet(key).decrypt(encrypted)
            return json.loads(payload.decode())
        except InvalidToken:
            raise ValueError("Decryption failed — incorrect key or corrupted store.")

    def get_credential(self, site: str, field: str, key: bytes) -> str:
        """
        Return a single credential field for a given site.

        :param site:  e.g. "linkedin", "naukri", "indeed"
        :param field: "username" or "password"
        :param key:   Fernet key bytes (loaded from env / secure vault at runtime)
        """
        store = self._load_all(key)
        if site not in store:
            raise KeyError(f"No credentials found for site: {site}")
        if field not in store[site]:
            raise KeyError(f"Field '{field}' not found for site: {site}")
        return store[site][field]

    def get_site_credentials(self, site: str, key: bytes) -> dict:
        """Return all credential fields for the given site as a dict."""
        store = self._load_all(key)
        if site not in store:
            raise KeyError(f"No credentials found for site: {site}")
        return store[site]

    def list_sites(self, key: bytes) -> list:
        """Return names of all stored sites."""
        return list(self._load_all(key).keys())

    # ------------------------------------------------------------------
    # Update helpers
    # ------------------------------------------------------------------

    def upsert_credential(self, site: str, field: str, value: str, key: bytes) -> None:
        """Add or update a single credential field without replacing the whole store."""
        store = self._load_all(key)
        store.setdefault(site, {})[field] = value
        payload = json.dumps(store).encode()
        encrypted = self._fernet(key).encrypt(payload)
        self.credentials_path.write_bytes(encrypted)
        self.credentials_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        print(f"[Agent4] Updated {site}.{field}")


# ---------------------------------------------------------------------------
# Convenience: load key from env at runtime
# ---------------------------------------------------------------------------

def load_key_from_env(env_var: str = "CRED_KEY") -> bytes:
    """
    Read the Fernet key from an environment variable.
    The value must be the base64-encoded key string printed by generate_key().
    """
    raw = os.environ.get(env_var, "")
    if not raw:
        raise EnvironmentError(
            f"Environment variable '{env_var}' is not set. "
            "Export the Fernet key before running."
        )
    return raw.encode()
