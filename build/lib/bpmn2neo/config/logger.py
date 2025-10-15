from __future__ import annotations

import json
import logging
import sys
from typing import Optional

class _JsonFormatter(logging.Formatter):
    """JSON formatter for structured logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        # Append common attributes if exist
        for k in ("asctime", "pathname", "lineno", "funcName"):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        # Include extra context
        if hasattr(record, "extra") and isinstance(record.extra, dict): # type: ignore
            payload.update(record.extra) # type: ignore
        return json.dumps(payload, ensure_ascii=False)


class Logger:
    """Singleton-style logger configurator and accessor."""
    
    _configured: bool = False
    
    @classmethod
    def configure(cls, level: str = "INFO") -> None:
        """Configure root logger only once."""
        if cls._configured:
            return

        log_level = getattr(logging, level.upper(), logging.INFO)

        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(log_level)
        handler.setFormatter(_JsonFormatter())

        root = logging.getLogger()
        root.setLevel(log_level)
        root.handlers.clear()
        root.addHandler(handler)

        cls._configured = True

    @classmethod
    def get_logger(cls, name: Optional[str] = None, level: str = "INFO") -> logging.Logger:
        """Return configured logger instance."""
        cls.configure(level=level)
        return logging.getLogger(name or "bpmn2neo")