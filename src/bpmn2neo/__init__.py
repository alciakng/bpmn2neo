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

__version__ = "0.2.0"
logger = Logger.get_logger("bpmn2neo")

__all__ = [
    "load_bpmn_to_neo4j",
    "create_node_embeddings",
    "load_and_embed",
    "create_category_node",
    "load_folder_to_neo4j",
    "Settings",
    "Loader",
    "Orchestrator",
]

def load_bpmn_to_neo4j(
    bpmn_path: str,
    model_key: Optional[str] = None,
    settings: Settings | None = None,
    parent_category_key: Optional[str] = None,
    predecessor_model_key: Optional[str] = None,
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
            summary: Dict[str, Any] = loader.load(
                bpmn_path=bpmn_path,
                model_key=model_key,
                parent_category_key=parent_category_key,
                predecessor_model_key=predecessor_model_key
            )
        else:
            mk_for_load = os.path.splitext(os.path.basename(bpmn_path))[0]
            summary = loader.load(
                bpmn_path=bpmn_path,
                model_key=mk_for_load,
                parent_category_key=parent_category_key,
                predecessor_model_key=predecessor_model_key
            )

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
    parent_category_key: Optional[str] = None,
    predecessor_model_key: Optional[str] = None,
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
            parent_category_key=parent_category_key,
            predecessor_model_key=predecessor_model_key,
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

def create_category_node(
    name: str,
    settings: Settings | None = None,
    parent_category_key: Optional[str] = None,
) -> Optional[str]:
    """
    Create a category node in Neo4j without loading a BPMN file.
    Category nodes are used to model hierarchical relationships between business processes.

    Args:
        name: Display name for the category node (also used as model_key)
        settings: Settings object (optional)
        parent_category_key: Parent category key (optional)

    Returns:
        The created model_key (same as name)
    """
    try:
        if settings is None:
            settings = Settings()

        logger.info(
            "[CATEGORY] API call",
            extra={"extra": {
                "name": name,
                "parent_category_key": parent_category_key
            }},
        )

        # Delegate to Loader.create_category_node
        loader = Loader(settings=settings)
        return loader.create_category_node(
            name=name,
            parent_category_key=parent_category_key
        )

    except Exception as e:
        logger.error("[CATEGORY] API failed", extra={"extra": {"err": str(e)}})
        raise

def load_folder_to_neo4j(
    folder_path: str,
    settings: Settings | None = None,
    parent_category_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Recursively load all BPMN files from a folder structure into Neo4j.

    Creates category nodes for folders and loads BPMN files as child models.
    Handles nested folder structures recursively.

    Args:
        folder_path: Path to the folder containing BPMN files and/or subfolders
        settings: Settings object (optional)
        parent_category_key: Parent category key for the root folder (optional)

    Returns:
        Dictionary containing loading statistics and results
    """
    try:
        if settings is None:
            settings = Settings()

        if not os.path.isdir(folder_path):
            raise ConfigError(f"Folder path does not exist or is not a directory: {folder_path}")

        logger.info(
            "[FOLDER] Starting folder load",
            extra={"extra": {
                "folder_path": folder_path,
                "parent_category_key": parent_category_key
            }}
        )

        results = {
            "folder_path": folder_path,
            "categories_created": [],
            "bpmn_files_loaded": [],
            "errors": [],
            "stats": {
                "total_categories": 0,
                "total_bpmn_files": 0,
                "successful_loads": 0,
                "failed_loads": 0
            }
        }

        # Create category node for this folder
        folder_name = os.path.basename(folder_path)
        folder_category_key = folder_name

        try:
            create_category_node(
                name=folder_name,
                settings=settings,
                parent_category_key=parent_category_key
            )
            results["categories_created"].append({
                "category_key": folder_category_key,
                "name": folder_name,
                "path": folder_path
            })
            results["stats"]["total_categories"] += 1
            logger.info(f"[FOLDER] Created category node: {folder_category_key} for {folder_name}")
        except Exception as e:
            error_msg = f"Failed to create category node for {folder_name}: {str(e)}"
            logger.error(error_msg)
            results["errors"].append(error_msg)

        # Get all items in folder
        try:
            items = sorted(os.listdir(folder_path))
        except Exception as e:
            error_msg = f"Failed to list directory {folder_path}: {str(e)}"
            logger.error(error_msg)
            results["errors"].append(error_msg)
            return results

        # Separate folders and BPMN files
        folders = []
        bpmn_files = []

        for item in items:
            item_path = os.path.join(folder_path, item)
            if os.path.isdir(item_path):
                folders.append(item_path)
            elif item.lower().endswith('.bpmn'):
                bpmn_files.append(item_path)

        # Process BPMN files in this folder
        prev_model_key = None
        for bpmn_file in bpmn_files:
            try:
                file_name = os.path.splitext(os.path.basename(bpmn_file))[0]
                file_model_key = file_name

                logger.info(f"[FOLDER] Loading BPMN file: {bpmn_file}")

                load_bpmn_to_neo4j(
                    bpmn_path=bpmn_file,
                    model_key=file_model_key,
                    settings=settings,
                    parent_category_key=folder_category_key,
                    predecessor_model_key=prev_model_key
                )

                results["bpmn_files_loaded"].append({
                    "model_key": file_model_key,
                    "name": file_name,
                    "path": bpmn_file,
                    "status": "success"
                })
                results["stats"]["total_bpmn_files"] += 1
                results["stats"]["successful_loads"] += 1

                prev_model_key = file_model_key
                logger.info(f"[FOLDER] Successfully loaded: {bpmn_file}")

            except Exception as e:
                error_msg = f"Failed to load BPMN file {bpmn_file}: {str(e)}"
                logger.error(error_msg)
                results["bpmn_files_loaded"].append({
                    "model_key": None,
                    "name": os.path.basename(bpmn_file),
                    "path": bpmn_file,
                    "status": "failed",
                    "error": str(e)
                })
                results["errors"].append(error_msg)
                results["stats"]["total_bpmn_files"] += 1
                results["stats"]["failed_loads"] += 1

        # Recursively process subfolders
        for subfolder in folders:
            try:
                logger.info(f"[FOLDER] Processing subfolder: {subfolder}")

                subfolder_result = load_folder_to_neo4j(
                    folder_path=subfolder,
                    settings=settings,
                    parent_category_key=folder_category_key
                )

                # Merge results
                results["categories_created"].extend(subfolder_result["categories_created"])
                results["bpmn_files_loaded"].extend(subfolder_result["bpmn_files_loaded"])
                results["errors"].extend(subfolder_result["errors"])
                results["stats"]["total_categories"] += subfolder_result["stats"]["total_categories"]
                results["stats"]["total_bpmn_files"] += subfolder_result["stats"]["total_bpmn_files"]
                results["stats"]["successful_loads"] += subfolder_result["stats"]["successful_loads"]
                results["stats"]["failed_loads"] += subfolder_result["stats"]["failed_loads"]

            except Exception as e:
                error_msg = f"Failed to process subfolder {subfolder}: {str(e)}"
                logger.error(error_msg)
                results["errors"].append(error_msg)

        logger.info(
            "[FOLDER] Folder load complete",
            extra={"extra": {
                "folder_path": folder_path,
                "categories": results["stats"]["total_categories"],
                "bpmn_files": results["stats"]["total_bpmn_files"],
                "successful": results["stats"]["successful_loads"],
                "failed": results["stats"]["failed_loads"]
            }}
        )

        return results

    except Exception as e:
        logger.error("[FOLDER] Folder load failed", extra={"extra": {"err": str(e)}})
        raise

