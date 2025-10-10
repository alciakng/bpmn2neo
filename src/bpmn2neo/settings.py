# bpmn2neo/settings.py
from __future__ import annotations

import os
from typing import Optional

try:
    import keyring  # Optional; do not hard-depend
except Exception:  # pragma: no cover
    keyring = None  # type: ignore

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from bpmn2neo.config.exceptions import ConfigError, SecretResolutionError
from bpmn2neo.config.logger import Logger


class Neo4jSettings(BaseModel):
    uri: str = Field(..., description="bolt[s]://host:port or neo4j[s]://host:port")
    username: str = Field(..., description="Neo4j username")
    password: Optional[str] = Field(None, description="Neo4j password (direct)")
    password_alias: Optional[str] = Field(
        None, description="If set, resolve secret via OS keyring. Example: 'bpmn2neo/neo4j'"
    )
    database: str = Field("neo4j", description="Neo4j database name")
    log_level: str = Field("INFO", description="Module log level")

class OpenAISettings(BaseModel):
    api_key: Optional[str] = Field(None, description="OpenAI API key (direct)")
    api_key_alias: Optional[str] = Field(
        None, description="If set, resolve secret via OS keyring. Example: 'bpmn2neo/openai'"
    )
    embedding_model: str = Field("text-embedding-3-large", description="Embedding model name")
    embedding_dimension: int = 3072
    translation_model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens_full: int = 600                # tokens reserved for FULL output
    max_tokens_summary: int = 200             # tokens reserved for SUMMARY output
    max_retries: int = Field(3, description="Max retry count")
    timeout: int = Field(60, description="HTTP timeout")
    log_level: str = Field("INFO", description="Module log level")

class ContainerSettings(BaseModel):
    create_container: bool = True
    container_type: str = Field("Project", description="Container Type")
    container_id: str = Field("Project", description="Container Id")
    container_name: str = Field("Project", description="Container Name")

class RuntimeSettings(BaseModel):
    log_level: str = Field("INFO", description="Global log level")
    parallelism: int = Field(1, description="Worker parallelism")
    batch_size: int = Field(64, description="Embedding batch size")
    cache_dir: Optional[str] = Field(None, description="Local cache directory")
    dry_run: bool = Field(False, description="Do not write to DB")
    fail_fast: bool = Field(False, description="Stop on first error")


class Settings(BaseSettings):
    """Unified runtime settings with layered sources.

    Order of precedence:
      1) Direct kwargs
      2) Environment variables (prefix B2N_)
      3) .env (if present)
      4) Defaults
    """

    neo4j: Neo4jSettings
    openai: OpenAISettings
    container: ContainerSettings = Field(default_factory=ContainerSettings)
    runtime: RuntimeSettings = RuntimeSettings()

    model_config = SettingsConfigDict(
        env_prefix = "B2N_",
        env_file = os.getenv("B2N_ENV_FILE", ".env"),
        env_nested_delimiter = "__"  # e.g., B2N_NEO4J__URI
    )

    def __init__(self, **values):
        # Ensure root logger is configured early
        Logger.configure(level=values.get("runtime", {}).get("log_level", "INFO") if isinstance(values.get("runtime"), dict) else "INFO")
        super().__init__(**values)
        self._logger = Logger.get_logger(self.__class__.__name__, level=self.runtime.log_level)
        self._logger.info("Settings loaded", extra={"extra": {"source": "env/.env/default"}})
        self._resolve_secrets()

    def _resolve_secrets(self) -> None:
        """Resolve secrets via keyring if alias fields are set."""
        try:
            if keyring is None:
                # If aliases are used but keyring is unavailable, raise explicit error.
                if any([
                    self.neo4j.password_alias,
                    self.openai.api_key_alias,
                ]):
                    raise SecretResolutionError("Keyring is not available; install 'keyring' or provide direct secrets.")

            # Neo4j password
            if self.neo4j.password_alias and not self.neo4j.password:
                self.neo4j.password = keyring.get_password(*self._split_alias(self.neo4j.password_alias))  # type: ignore
            # OpenAI key
            if self.openai.api_key_alias and not self.openai.api_key:
                self.openai.api_key = keyring.get_password(*self._split_alias(self.openai.api_key_alias))  # type: ignore
            
            # Validate critical secrets exist
            if not self.neo4j.password:
                raise ConfigError("Neo4j password is not set (direct or via keyring).")
            if not self.openai.api_key:
                raise ConfigError("OpenAI API key is not set (direct or via keyring).")

            self._logger.info("Secrets resolved successfully")

        except Exception as e:
            self._logger.error("Secret resolution failed", extra={"extra": {"err": str(e)}})
            raise

    @staticmethod
    def _split_alias(alias: str) -> tuple[str, str]:
        """Split 'service/username' into tuple for keyring."""
        try:
            service, username = alias.split("/", 1)
            return service, username
        except Exception as e:
            raise SecretResolutionError(f"Invalid alias format: {alias!r}. Expected 'service/username'.") from e
