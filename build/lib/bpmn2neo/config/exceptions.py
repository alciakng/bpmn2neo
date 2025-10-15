# bpmn2neo/exceptions.py
from __future__ import annotations


class Bpmn2NeoError(Exception):
    """Base exception for the library."""


class ConfigError(Bpmn2NeoError):
    """Invalid or missing configuration."""


class SecretResolutionError(Bpmn2NeoError):
    """Keyring/secret resolution failed."""


class Neo4jRepositoryError(Bpmn2NeoError):
    """Repository-level errors (connectivity, query, transaction)."""


class BpmnParseError(Bpmn2NeoError):
    """BPMN parsing errors."""


class LlmError(Bpmn2NeoError):
    """LLM/ContextWriter related errors."""


class EmbeddingError(Bpmn2NeoError):
    """Embedding service errors."""
