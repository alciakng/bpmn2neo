# utils/keyring.py
from __future__ import annotations

import os
from typing import Dict, Iterable, List, Optional

from bpmn2neo.config.exceptions import SecretResolutionError, ConfigError
from bpmn2neo.config.logger import Logger

try:
    import keyring  # Optional: OS keychain backend
except Exception:  # noqa: BLE001
    keyring = None  # Allow usage without keyring installed


# Minimal env alias map for common secret keys
_DEFAULT_ENV_ALIASES: Dict[str, List[str]] = {
    "openai_api_key": ["OPENAI_API_KEY"],
    "neo4j_uri": ["NEO4J_URI", "NEO4J_URL"],
    "neo4j_username": ["NEO4J_USERNAME", "NEO4J_USER"],
    "neo4j_password": ["NEO4J_PASSWORD"],
    "s3_endpoint_url": ["S3_ENDPOINT_URL"],
    "aws_access_key_id": ["AWS_ACCESS_KEY_ID", "MINIO_ROOT_USER"],
    "aws_secret_access_key": ["AWS_SECRET_ACCESS_KEY", "MINIO_ROOT_PASSWORD", "MINIO_SECRET_KEY"],
}

class SecretResolver:
    """
    Resolve secrets with this priority:
      1) Environment variables (explicit names first)
      2) OS keyring (if available)
      3) Config-provided dict (e.g., Settings.secrets)
    """

    def __init__(self, namespace: str = "bpmn2neo", config_secrets: Optional[Dict[str, str]] = None) -> None:
        self.logger = Logger.get_logger(self.__class__.__name__)
        self.namespace = namespace
        self.config_secrets = {k.lower(): v for k, v in (config_secrets or {}).items()}

    # -----------------------------
    # Public API
    # -----------------------------
    def get_secret(
        self,
        key: str,
        env_names: Optional[Iterable[str]] = None,
        required: bool = True,
    ) -> Optional[str]:
        """
        Resolve a secret value by key.
        Args:
            key: logical secret key (e.g., "openai_api_key", "neo4j_password")
            env_names: extra env var names to check first (highest priority)
            required: if True, raise SecretResolutionError when not found
        """
        lkey = key.lower()
        self.logger.info("[KEYRING] get_secret start key=%s", lkey)

        # 1) Environment variables (explicit > aliases)
        try:
            # Explicit names take top priority
            names_to_check: List[str] = []
            if env_names:
                names_to_check.extend([str(x) for x in env_names])

            # Known aliases (by logical key)
            names_to_check.extend(_DEFAULT_ENV_ALIASES.get(lkey, []))

            # Heuristic: also check UPPER_SNAKE of the logical key
            heuristic = lkey.upper()
            if heuristic not in names_to_check:
                names_to_check.append(heuristic)

            for env_name in names_to_check:
                val = os.getenv(env_name)
                if val:
                    self.logger.info("[KEYRING] resolved via env var name=%s", env_name)
                    return val
        except Exception as e:  # noqa: BLE001
            self.logger.exception("[KEYRING] env_check unexpected_error key=%s", lkey)
            # Continue to other sources, do not raise yet

        # 2) OS keyring
        try:
            if keyring is not None:
                # Use logical key as username; namespace as service name
                secret = keyring.get_password(self.namespace, lkey)
                if secret:
                    self.logger.info("[KEYRING] resolved via os_keyring service=%s", self.namespace)
                    return secret
            else:
                self.logger.debug("[KEYRING] os_keyring not available (module missing)")
        except Exception as e:  # noqa: BLE001
            self.logger.exception("[KEYRING] os_keyring unexpected_error key=%s", lkey)
            # Continue to config

        # 3) Config dict (if supplied)
        try:
            if lkey in self.config_secrets and self.config_secrets[lkey]:
                self.logger.info("[KEYRING] resolved via config_secrets key=%s", lkey)
                return self.config_secrets[lkey]
        except Exception as e:  # noqa: BLE001
            self.logger.exception("[KEYRING] config_lookup unexpected_error key=%s", lkey)

        # Not found
        msg = f"Secret not found: {key} (checked env, keyring, config)"
        self.logger.error("[KEYRING] resolve_failed key=%s", lkey)
        if required:
            raise SecretResolutionError(msg)
        return None

    def set_secret(self, key: str, value: str) -> None:
        """
        Store a secret into OS keyring (developer convenience).
        Raises SecretResolutionError if keyring backend is not available.
        """
        lkey = key.lower()
        if not value:
            raise ConfigError("Refusing to store empty secret value")

        if keyring is None:
            self.logger.error("[KEYRING] set_secret failed reason=keyring_unavailable key=%s", lkey)
            raise SecretResolutionError("keyring backend not available (install 'keyring')")

        try:
            keyring.set_password(self.namespace, lkey, value)
            self.logger.info("[KEYRING] set_secret ok service=%s key=%s", self.namespace, lkey)
        except Exception as e:  # noqa: BLE001
            self.logger.exception("[KEYRING] set_secret unexpected_error key=%s", lkey)
            raise SecretResolutionError(str(e)) from e
