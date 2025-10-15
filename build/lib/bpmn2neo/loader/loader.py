# core/bpmn_loader.py
import os
import tempfile

from typing import List, Dict, Any, Optional
from bpmn2neo.config.exceptions import Bpmn2NeoError, BpmnParseError, ConfigError, Neo4jRepositoryError
from bpmn2neo.settings import Settings
from bpmn2neo.config.logger import Logger
from bpmn2neo.config.neo4j_repo import Neo4jRepository, CypherBuilder
from bpmn2neo.loader.parser import Parser

class Loader:
    """
    Main BPMN loader class (SRP): orchestrates parsing and persists into Neo4j.
    - Dependency-injected Settings and (optionally) Neo4jRepository.
    - Structured logging through self.logger with [LOAD] prefix.
    - Defensive try/except at each critical step for robust error reporting.
    """

    def __init__(self, settings: Settings, repository: Optional[Neo4jRepository] = None):
        """
        Initialize loader with settings and optional repository (DI).
        If repository is not provided, instantiate internally for backward-compatibility.
        """
        self.settings = settings
        self.logger = Logger.get_logger(self.__class__.__name__)
        try:
            # Repository dependency injection (prefer injected instance).
            self.repository = repository if repository is not None else Neo4jRepository(settings.neo4j)
            self.logger.info("[LOAD][INIT] Neo4jRepository initialized")
        except Exception as e:
            self.logger.exception("[LOAD][INIT] Failed to initialize Neo4jRepository")
            raise

        self.parser = None
        self._schema_ready = False

    def load(
        self,
        *,
        bpmn_path: str,
        model_key: str,
    ) -> Dict[str, Any]:
        """
        Thin wrapper orchestrating:
        1) Cleanup existing graph for the model_key (keeps Container nodes).
        2) Read BPMN XML via IOHelper (local or s3/minio) and materialize to a local temp file.
        3) Delegate to self.load_bpmn_file(<resolved local path>, model_key).

        Notes:
        - Keeps function minimal as requested; no extra helpers unless necessary.
        - Uses structured logging and precise domain exceptions.
        """
        logger = self.logger

        # --- Basic validation ---
        if not bpmn_path or not isinstance(bpmn_path, str):
            logger.error("[LOADER] load invalid bpmn_path_or_uri type=%s", type(bpmn_path))
            raise ConfigError("Invalid BPMN path/URI")
        if not model_key:
            logger.error("[LOADER] load missing model_key")
            raise ConfigError("model_key is required")

        logger.info(
            "[LOADER] load start model_key=%s src=%s",
            model_key, bpmn_path
        )

        # --- 1) Cleanup (preserve Container) ---
        try:
            if not hasattr(self, "cleanup_models"):
                logger.error("[LOADER] cleanup_models not found on Loader")
                raise ConfigError("cleanup_models is not available on Loader")
            deleted = self.cleanup_models([model_key])  # 기존에 정의되어 있는 함수를 그대로 사용
            logger.info("[LOADER] cleanup ok model_key=%s deleted=%s", model_key, deleted)
        except (Neo4jRepositoryError, ConfigError):
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("[LOADER] cleanup unexpected_error model_key=%s", model_key)
            raise Neo4jRepositoryError(f"Cleanup failed: {e}") from e

        # --- 2) Delegate to existing loader flow that parses & ingests ---
        try:
            # NOTE: self.load_bpmn_file(...)는 기존에 구현되어 있는 "Parser 초기화 후 적재" 루틴입니다.
            if not hasattr(self, "load_bpmn_file"):
                logger.error("[LOADER] load_bpmn_file not found on Loader")
                raise ConfigError("load_bpmn_file is not available on Loader")

            result = self.load_bpmn_file(bpmn_path, model_key)  # 기존 함수 시그니처에 맞춰 전달
            logger.info("[LOADER] load done model_key=%s result=%s", model_key, result)
            return result if isinstance(result, dict) else {"model_key": model_key, "status": "ok"}
        except (BpmnParseError, Neo4jRepositoryError, ConfigError, Bpmn2NeoError):
            # Upper layers will handle; we already logged with context
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("[LOADER] load_bpmn_file unexpected_error model_key=%s", model_key)
            raise Bpmn2NeoError(str(e)) from e


    def _ensure_schema(self):
        """Create constraints/indexes once before any load."""
        if self._schema_ready:
            self.logger.info("[LOAD][SCHEMA] Already ensured; skipping")
            return

        self.logger.info("[LOAD][SCHEMA] Ensuring Neo4j schema (constraints/indexes)")
        stmts = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:BPMNModel)         REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Participant)       REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Process)           REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Lane)              REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Activity)          REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Event)             REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Gateway)           REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Data)              REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:DataRefernce)      REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:GROUP)             REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:TextAnnotation)    REQUIRE (n.id, n.modelKey) IS NODE KEY",
        ]
        try:
            queries = [(q, {}) for q in stmts]
            self.repository.execute_queries(queries)
            self.logger.info("[LOAD][SCHEMA] Constraints/indexes created (if not exists)")
        except Exception:
            self.logger.exception("[LOAD][SCHEMA] Failed to create constraints/indexes")
            raise

        try:
            self.repository.execute_single_query("CALL db.awaitIndexes()")
            self.logger.info("[LOAD][SCHEMA] Indexes online")
        except Exception:
            self.logger.exception("[LOAD][SCHEMA] Failed awaiting indexes")
            raise

        self._schema_ready = True
        self.logger.info("[LOAD][SCHEMA] Ensured")

    def load_bpmn_file(
        self,
        xml_file_path: str,
        model_key: str
    ) -> Dict[str, Any]:
        """
        Parse a BPMN XML file and load nodes/relationships into Neo4j.

        Args:
            xml_file_path: Local filesystem path to BPMN XML.
            clear_type: Cleanup policy ("none", "model", "container", "all").
            container_type: Container type ("Project", "Question").
            container_id: Container id for scoping.
            container_name: Human-readable container name.

        Returns:
            Result dict containing status, counts, and model keys.
        """
        # Initialize parser with container scope
        try:
            self.parser = Parser(self.settings.container)
            self.logger.info("[LOAD][INIT] Parser initialized")
        except Exception as e:
            self.logger.exception("[LOAD][INIT] Failed to initialize Parser")
            return {
                "status": "error",
                "error_message": f"Parser init failed: {e}",
                "xml_file": xml_file_path,
            }

        self.logger.info("[LOAD] Start", extra={"extra": {"file": xml_file_path}})

        # 1) Parse XML
        try:
            self.logger.info("[LOAD][PARSE] Begin")
            nodes, relationships = self.parser.parse(xml_file_path, model_key)
            model_keys = self.parser.get_model_keys()
            self.logger.info(
                "[LOAD][PARSE] Done",
                extra={"extra": {"nodes": len(nodes), "rels": len(relationships), "models": len(model_keys)}},
            )
        except Exception as e:
            self.logger.exception("[LOAD][PARSE] Failed")
            return {
                "status": "error",
                "error_message": f"Parsing failed: {e}",
                "xml_file": xml_file_path,
            }

        # 2) Ensure schema once
        try:
            self.logger.info("[LOAD][SCHEMA] Begin")
            self._ensure_schema()
            self.logger.info("[LOAD][SCHEMA] Done")
        except Exception as e:
            self.logger.exception("[LOAD][SCHEMA] Failed")
            return {
                "status": "error",
                "error_message": f"Schema ensure failed: {e}",
                "xml_file": xml_file_path,
            }

        # 3) Load into Neo4j (batch)
        try:
            self.logger.info("[LOAD][WRITE] Begin")
            self._load_to_neo4j(nodes, relationships)
            self.logger.info("[LOAD][WRITE] Done")
        except Exception as e:
            self.logger.exception("[LOAD][WRITE] Failed")
            return {
                "status": "error",
                "error_message": f"Neo4j load failed: {e}",
                "xml_file": xml_file_path,
            }

        # Build and return successful result
        result = {
            "status": "success",
            "xml_file": xml_file_path,
            "stats": {
                "nodes_count": len(nodes),
                "relationships_count": len(relationships),
                "model_keys_count": len(model_keys),
            },
            "model_keys": model_keys,
        }

        self.logger.info(
            "[LOAD] Finished",
            extra={
                "extra": {
                    "nodes": len(nodes),
                    "rels": len(relationships),
                    "models": len(model_keys),
                }
            },
        )
        return result

    def load_multiple_files(
        self,
        file_configs: List[Dict[str, Any]],
        global_clear_type: str = "none",
    ) -> List[Dict[str, Any]]:
        """
        Load multiple BPMN files sequentially.

        Args:
            file_configs: List of file config dicts (see single-load docstring).
            global_clear_type: Global cleanup policy.

        Returns:
            List of per-file load result dicts.
        """
        results: List[Dict[str, Any]] = []
        self.logger.info("[LOAD][BATCH] Start", extra={"extra": {"count": len(file_configs)}})

        for i, config in enumerate(file_configs, 1):
            try:
                self.logger.info("[LOAD][BATCH] File begin", extra={"extra": {"index": i, "file": config.get("xml_file_path")}})
                xml_file_path = config["xml_file_path"]
                container_type = config.get("container_type", "Project")
                container_id = config.get("container_id", f"auto-{i}")
                container_name = config.get("container_name", f"Auto Container {i}")
                clear_type = config.get("clear_type", global_clear_type)

                res = self.load_bpmn_file(
                    xml_file_path=xml_file_path
                )
                res["file_index"] = i
                results.append(res)

                if res.get("status") == "error":
                    self.logger.error("[LOAD][BATCH] File failed; continue", extra={"extra": {"index": i}})
                else:
                    self.logger.info("[LOAD][BATCH] File done", extra={"extra": {"index": i}})

            except KeyError as e:
                self.logger.exception("[LOAD][BATCH] Invalid file config")
                results.append(
                    {
                        "status": "error",
                        "error_message": f"Invalid config for file {i}: missing {e}",
                        "file_index": i,
                        "xml_file": config.get("xml_file_path"),
                    }
                )
            except Exception as e:
                self.logger.exception("[LOAD][BATCH] Unexpected error")
                results.append(
                    {
                        "status": "error",
                        "error_message": f"Unexpected error: {e}",
                        "file_index": i,
                        "xml_file": config.get("xml_file_path"),
                    }
                )

        success_count = sum(1 for r in results if r.get("status") == "success")
        self.logger.info("[LOAD][BATCH] Finished", extra={"extra": {"success": success_count, "total": len(file_configs)}})
        return results

    def get_loading_stats(self) -> Dict[str, Any]:
        """Return loading stats for current container scope."""
        if not self.parser:
            return {"status": "no_parser", "message": "Parser is not initialized"}

        try:
            self.logger.info("[LOAD][STATS] Node stats begin")
            node_stats = self.repository.execute_single_query(
                """
                MATCH (n) 
                WHERE n.containerId IS NOT NULL
                RETURN n.containerId as containerId, 
                       labels(n)[0] as nodeType, 
                       count(*) as count
                ORDER BY containerId, nodeType
                """
            )
            self.logger.info("[LOAD][STATS] Node stats done", extra={"extra": {"rows": len(node_stats)}})
        except Exception as e:
            self.logger.exception("[LOAD][STATS] Node stats failed")
            return {"status": "error", "error_message": f"Node stats failed: {e}"}

        try:
            self.logger.info("[LOAD][STATS] Rel stats begin")
            rel_stats = self.repository.execute_single_query(
                """
                MATCH ()-[r]->() 
                WHERE r.containerId IS NOT NULL
                RETURN r.containerId as containerId,
                       type(r) as relType,
                       count(*) as count
                ORDER BY containerId, relType
                """
            )
            self.logger.info("[LOAD][STATS] Rel stats done", extra={"extra": {"rows": len(rel_stats)}})
        except Exception as e:
            self.logger.exception("[LOAD][STATS] Rel stats failed")
            return {"status": "error", "error_message": f"Relationship stats failed: {e}"}

        try:
            self.logger.info("[LOAD][STATS] Parser model keys begin")
            model_keys = self.parser.get_model_keys()
            self.logger.info("[LOAD][STATS] Parser model keys done", extra={"extra": {"count": len(model_keys)}})
        except Exception as e:
            self.logger.exception("[LOAD][STATS] Parser model keys failed")
            return {"status": "error", "error_message": f"Parser model keys failed: {e}"}

        return {
            "status": "success",
            "node_statistics": node_stats,
            "relationship_statistics": rel_stats,
            "parser_model_keys": model_keys,
        }

    def cleanup_models(self, model_keys: List[str]) -> bool:
        """Remove all data for given model keys."""
        try:
            self.logger.info("[LOAD][CLEANUP] Models begin", extra={"extra": {"count": len(model_keys)}})
            self.repository.clear_data(model_keys)
            self.logger.info("[LOAD][CLEANUP] Models done", extra={"extra": {"count": len(model_keys)}})
            return True
        except Exception as e:
            self.logger.error(f"[LOAD][CLEANUP] Models failed: {e}")
            return False

    def close(self):
        """Release resources gracefully."""
        try:
            if self.repository:
                self.logger.info("[LOAD][SHUTDOWN] Closing repository")
                self.repository.close()
        except Exception as e:
            self.logger.error(f"[LOAD][SHUTDOWN] Repository close failed: {e}")
        finally:
            self.logger.info("[LOAD][SHUTDOWN] BPMN Loader shut down")

    def _load_to_neo4j(self, nodes: List[Dict[str, Any]], relationships: List[Dict[str, Any]]):
        """Internal helper: persist nodes and relationships to Neo4j in batch."""
        self.logger.info("[LOAD][WRITE] Neo4j load start")

        # Prepare Cypher queries
        try:
            queries = []
            for node in nodes:
                q = CypherBuilder.create_node_query(node)
                queries.append(q)
            for relationship in relationships:
                q = CypherBuilder.create_relationship_query(relationship)
                queries.append(q)
            self.logger.info("[LOAD][WRITE] Built Cypher queries", extra={"extra": {"count": len(queries)}})
        except Exception:
            self.logger.exception("[LOAD][WRITE] Failed to build Cypher queries")
            raise

        # Execute batch
        try:
            execute_queries = [(q, {}) for q in queries]
            self.repository.execute_queries(execute_queries)
            self.logger.info("[LOAD][WRITE] Batch executed")
        except Exception:
            self.logger.exception("[LOAD][WRITE] Failed to execute batch queries")
            raise

        self.logger.info("[LOAD][WRITE] Neo4j load done")
