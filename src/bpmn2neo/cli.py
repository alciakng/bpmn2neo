# bpmn2neo/cli.py
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from bpmn2neo.config.logger import Logger
from bpmn2neo.settings import Settings
from . import load_bpmn_to_neo4j, create_node_embeddings


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bpmn2neo", description="BPMN → Neo4j loader and embedding generator")
    sub = p.add_subparsers(dest="cmd", required=True)

    # load
    p_load = sub.add_parser("load", help="Load BPMN into Neo4j")
    p_load.add_argument("--bpmn", required=True, help="Path or URI to .bpmn file")
    p_load.add_argument("--model-key", required=True, help="Model key (namespace for nodes)")

    # embed
    p_embed = sub.add_parser("embed", help="Create node texts and embeddings")
    p_embed.add_argument("--model-key", required=True, help="Model key")
    p_embed.add_argument("--levels", nargs="*", default=[], help="Subset of levels: flownode lane process participant model")

    # run (load + embed)
    p_run = sub.add_parser("run", help="Load then embed")
    p_run.add_argument("--bpmn", required=True, help="Path or URI to .bpmn file")
    p_run.add_argument("--model-key", required=True, help="Model key")
    p_run.add_argument("--levels", nargs="*", default=[], help="Subset of levels")

    return p


def _load_settings() -> Settings:
    """Load Settings from env/.env (most common in production)."""
    # Let pydantic read env; if user needs direct kwargs, they can write a tiny wrapper.
    return Settings()


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Configure global logger now; Settings will refine level if provided in env
    Logger.configure("INFO")

    try:
        settings = _load_settings()

        if args.cmd == "load":
            load_bpmn_to_neo4j(bpmn_path_or_uri=args.bpmn, model_key=args.model_key, settings=settings)
            return 0

        if args.cmd == "embed":
            create_node_embeddings(model_key=args.model_key, settings=settings, levels=args.levels)
            return 0

        if args.cmd == "run":
            load_bpmn_to_neo4j(bpmn_path_or_uri=args.bpmn, model_key=args.model_key, settings=settings)
            create_node_embeddings(model_key=args.model_key, settings=settings, levels=args.levels)
            return 0

        parser.print_help()
        return 1

    except Exception as e:
        Logger.get_logger("bpmn2neo.cli").error("Command failed", extra={"extra": {"err": str(e)}})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
