# src/bpmn2neo/__init__.py
from __future__ import annotations

from typing import Optional, Dict, Any
import os

from bpmn2neo.config.exceptions import ConfigError
from bpmn2neo.config.logger import Logger
from bpmn2neo.settings import Settings

# Core components
from bpmn2neo.loader.loader import Loader
from bpmn2neo.embedder.orchestrator import Orchestrator

__version__ = "0.1.8"
logger = Logger.get_logger("bpmn2neo")

def load_bpmn_to_neo4j(
    bpmn_path: str,
    model_key: Optional[str] = None,
    settings: Settings | None = None,
) -> Optional[str]:
    """
    High-level API: Parse a BPMN file and persist into Neo4j.

    Behavior:
    - If 'model_key' is provided (not None/empty), forward it to Loader so parser uses it.
    - If not provided, Loader/Parser will derive model_key from file name.
    - Write the full loader summary to structured logs, but RETURN ONLY the resolved model_key (first one) or None.
    """
    try:
        if settings is None:
            settings = Settings()

        logger.info(
            "[01.LOAD] start",
            extra={"extra": {
                "provided_model_key": model_key,
                "src": bpmn_path
            }},
        )

        loader = Loader(settings=settings)

        # Call loader with or without model_key depending on user input
        if model_key:
            summary: Dict[str, Any] = loader.load(bpmn_path=bpmn_path, model_key=model_key)
        else:
            mk_for_load = os.path.splitext(os.path.basename(bpmn_path))[0]
            summary = loader.load(bpmn_path=bpmn_path,model_key=mk_for_load)

        # Extract fields for logging
        stats = (summary or {}).get("stats") or {}
        model_keys = (summary or {}).get("model_keys") or []
        model_key = model_keys[0] if model_keys else None

        logger.info(
            "[01.LOAD] done",
            extra={"extra": {
                "model_key": model_key,
                "model_keys_count": len(model_keys),
                "nodes_count": stats.get("nodes_count"),
                "relationships_count": stats.get("relationships_count"),
                "xml_file": (summary or {}).get("xml_file")
            }},
        )

        # Return only the resolved model key (first one) to downstream
        return model_key

    except Exception as e:
        logger.error("[01.LOAD] failed", extra={"extra": {"err": str(e)}})
        raise

def create_node_embeddings(
    model_key: str,
    settings: Settings | None = None,
    mode: str = "all",
) -> Dict[str, Any]:
    """
    Create node embeddings for a given model.
    mode:
      - "all"   : full pipeline (FlowNodes → Lanes → Process → Participant → Model)
      - "light" : FlowNodes only (fast iteration)
    """
    try:
        cfg = settings or Settings()
        orch = Orchestrator(cfg, logger=Logger.get_logger("Orchestrator"))

        if mode == "light":
            return orch.embed_flownode_only(model_key=model_key)
        if mode == "all":
            return orch.run_all(model_key=model_key)
        raise ConfigError(f"Invalid mode: {mode}. Use 'all' or 'light'.")
    except Exception as e:
        logger.exception("[EMBED] failed: %s", e)
        raise

def load_and_embed(
    *,
    bpmn_path: Optional[str] = None,
    model_key: Optional[str] = None,
    settings: Optional[Settings] = None,
    mode: str = "all",
) -> Dict[str, Any]:
    """
    Convenience API: Run both 'load' and 'embed' in one call.

    - If 'settings' is None, instantiate from env/.env.
    - If 'bpmn_path' is None, try to read from settings.runtime.bpmn_file.
    - For embedding, prefer model_key returned from load_bpmn_to_neo4j().
      If not present, fall back to (explicit model_key) or filename stem.
        mode:
        - "all"   : embed full pipeline (FlowNodes → Lanes → Process → Participant → Model)
        - "light" : embed FlowNodes only (fast iteration)
    """
    try:
        cfg = settings or Settings()

        # Resolve BPMN path
        bpmn_path = bpmn_path or getattr(getattr(cfg, "runtime", None), "bpmn_file", None)
        if not bpmn_path:
            raise ConfigError("BPMN path is not provided (arg or B2N_RUNTIME__BPMN_FILE).")

        # 1) LOAD: returns ONLY the resolved model_key (or None)
        model_key_from_load: Optional[str] = load_bpmn_to_neo4j(
            bpmn_path=bpmn_path,
            model_key=model_key if model_key else None,
            settings=cfg,
        )

        # 2) Resolve model_key for embedding
        #    Priority: returned key from load -> explicit model_key -> filename stem
        if model_key_from_load:
            mk_for_embed = model_key_from_load
        elif model_key:
            mk_for_embed = model_key
        else:
            mk_for_embed = os.path.splitext(os.path.basename(bpmn_path))[0]

        # 3) EMBED
        embed_summary = create_node_embeddings(model_key=mk_for_embed, settings=cfg, mode=mode)

        return {"embed": embed_summary, "model_key": mk_for_embed}

    except Exception as e:
        logger.error("[PIPELINE] failed", extra={"extra": {"err": str(e)}})
        raise
    
