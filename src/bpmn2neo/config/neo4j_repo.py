# bpmn2neo/repository.py
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from neo4j import GraphDatabase, basic_auth, Driver

from bpmn2neo.config.exceptions import Neo4jRepositoryError
from bpmn2neo.config.logger import Logger
from bpmn2neo.settings import Neo4jSettings

class Neo4jRepository:
    """Thin repository wrapper around the Neo4j driver."""

    def __init__(self, cfg: Neo4jSettings):
        self.cfg = cfg
        self.logger = Logger.get_logger(self.__class__.__name__, level=cfg.log_level)
        try:
            self.driver: Driver = GraphDatabase.driver(
                cfg.uri,
                auth=basic_auth(cfg.username, cfg.password or ""),
            )
            # Verify connectivity early
            self.driver.verify_authentication()
            self._ensure_schema()
            self.logger.info("Neo4j driver initialized", extra={"extra": {"uri": cfg.uri, "db": cfg.database}})
        except Exception as e:
            self.logger.error("Neo4j driver init failed", extra={"extra": {"err": str(e)}})
            raise Neo4jRepositoryError(str(e)) from e

    def close(self) -> None:
        try:
            self.driver.close()
            self.logger.info("Neo4j driver closed")
        except Exception as e:
            self.logger.error("Neo4j driver close failed", extra={"extra": {"err": str(e)}})

    def _ensure_schema(self) -> None:
        """
        Create constraints/indexes once before any load.

        Notes:
        - Uses IF NOT EXISTS so repeated calls are safe (no-ops).
        - Uses named constraints for easier ops/maintenance.
        - Avoids wrapping all DDL in one explicit transaction (neo4j best-effort).
        - Gracefully handles permission/feature issues and keeps the app running.
        """
        self.logger.info("Ensuring Neo4j schema (constraints/indexes)...")

        # IMPORTANT:
        # - Keep label names consistent with your data model.
        # - If your data already uses an accidental label (e.g., 'DataRefernce'),
        #   either fix the data generator or add a second constraint for that label.
        stmts = [
            "CREATE CONSTRAINT bpmn_model_nodekey     IF NOT EXISTS FOR (n:BPMNModel)      REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT participant_nodekey    IF NOT EXISTS FOR (n:Participant)     REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT process_nodekey        IF NOT EXISTS FOR (n:Process)         REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT lane_nodekey           IF NOT EXISTS FOR (n:Lane)            REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT activity_nodekey       IF NOT EXISTS FOR (n:Activity)        REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT event_nodekey          IF NOT EXISTS FOR (n:Event)           REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT gateway_nodekey        IF NOT EXISTS FOR (n:Gateway)         REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT data_nodekey           IF NOT EXISTS FOR (n:Data)            REQUIRE (n.id, n.modelKey) IS NODE KEY",
            # Prefer the corrected label 'DataReference'. If legacy data used a misspelled label,
            # either migrate the data or add another constraint specifically for that label.
            "CREATE CONSTRAINT dataref_nodekey        IF NOT EXISTS FOR (n:DataReference)   REQUIRE (n.id, n.modelKey) IS NODE KEY",
            # If your model truly uses 'GROUP' as a label, keep this. Otherwise align casing (e.g., 'Group').
            "CREATE CONSTRAINT group_nodekey          IF NOT EXISTS FOR (n:GROUP)           REQUIRE (n.id, n.modelKey) IS NODE KEY",
            "CREATE CONSTRAINT textanno_nodekey       IF NOT EXISTS FOR (n:TextAnnotation)  REQUIRE (n.id, n.modelKey) IS NODE KEY",
            # Optional: add secondary indexes on modelKey when you frequently filter/cleanup by modelKey
            # "CREATE INDEX activity_modelkey_idx IF NOT EXISTS FOR (n:Activity) ON (n.modelKey)",
        ]

        # Run each DDL with autocommit; do not fail the app if one statement is not allowed.
        for cypher in stmts:
            try:
                self.execute_single_query(cypher)
            except Exception as e:
                # Permissions or version compatibility issues should not crash the app.
                self.logger.warning("Schema DDL skipped/failed: %s | %s", cypher, str(e))

        # Wait for index/constraint online if the DB supports it.
        try:
            self.execute_single_query("CALL db.awaitIndexes()")
        except Exception as e:
            self.logger.warning("db.awaitIndexes() failed/unsupported: %s", str(e))

        self._schema_ready = True
        self.logger.info("Neo4j schema ensured.")

    def execute_single_query(self, query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Execute a single Cypher query and return list of dict records."""
        params = params or {}
        self.logger.info("[CYPHER][SINGLE] running", extra={"extra": {"q": self._short(query)}})
        try:
            with self.driver.session(database=self.cfg.database) as session:  
                result = session.run(query, **params)
                rows = [r.data() for r in result]
                self.logger.info("[CYPHER][SINGLE] done", extra={"extra": {"rows": len(rows)}})
                return rows
        except Exception as e:
            self.logger.error("[CYPHER][SINGLE] failed", extra={"extra": {"err": str(e)}})
            raise Neo4jRepositoryError(str(e)) from e

    def execute_queries(self, queries: Iterable[Tuple[str, Dict[str, Any]]]) -> None:
        """Execute multiple queries in a single write transaction."""
        self.logger.info("[CYPHER][BATCH] starting")
        try:
            with self.driver.session(database=self.cfg.database) as session:

                def _tx_run(tx, qps: Iterable) -> None:
                    for item in qps:
                        try:
                            # --- normalize item to (query:str, params:dict) ---
                            if isinstance(item, tuple):
                                if len(item) == 2:
                                    q, p = item
                                elif len(item) == 1:
                                    q, p = item[0], {}
                                else:
                                    q, p = item[0], (item[1] if isinstance(item[1], dict) else {})
                                    # Log that extra elements were ignored
                                    self.logger.warning(
                                        "[CYPHER][BATCH] extra tuple elements ignored",
                                        extra={"extra": {"tuple_len": len(item)}},
                                    )
                            elif isinstance(item, str):
                                q, p = item, {}
                            else:
                                raise TypeError(f"Invalid query item type: {type(item).__name__}")

                            tx.run(q, **(p or {}))

                        except Exception as e:
                            # Log per-item failure with a short preview for debugging
                            self.logger.exception(
                                "[CYPHER][BATCH] item failed",
                                extra={"extra": {"item_preview": str(item)[:200]}},
                            )
                            raise

                session.execute_write(_tx_run, queries)

            self.logger.info("[CYPHER][BATCH] committed")
        except Exception as e:
            self.logger.error("[CYPHER][BATCH] failed", extra={"extra": {"err": str(e)}})
            raise Neo4jRepositoryError(str(e)) from e

        

    def clear_data(self, identifiers: List[str] = None):
        """
        데이터 정리
        clear_type: "all" | "container" | "model" | "none"
        identifiers: container_id 또는 model_keys 리스트
        """
        
        try:
            with self.driver.session(database=self.cfg.database) as session:
                
                model_keys = identifiers
                self.logger.info(f"모델 {len(model_keys)}개 삭제 중...")
                
                result1 = session.run("MATCH ()-[r]-() WHERE r.modelKey IN $mks DELETE r", mks=model_keys)
                self.logger.info(f"관계 삭제 완료: {result1.consume().counters}")
                
                result2 = session.run("MATCH (n) WHERE n.modelKey IN $mks DETACH DELETE n", mks=model_keys)
                self.logger.info(f"노드 삭제 완료: {result2.consume().counters}")
                    
        except Exception as e:
            self.logger.error(f"데이터 정리 실패: {e}")
            raise

    @staticmethod
    def _short(s: str, n: int = 200) -> str:
        return " ".join(s.split())[:n] + ("..." if len(" ".join(s.split())) > n else "")

    @staticmethod
    def _safe(params: Dict[str, Any]) -> Dict[str, Any]:
        # Do not log secrets accidentally
        masked = {}
        for k, v in params.items():
            if any(t in k.lower() for t in ("password", "secret", "token", "key")) and isinstance(v, str):
                masked[k] = "***"
            else:
                masked[k] = v
        return masked



class CypherBuilder:
    """Cypher 쿼리 생성 유틸리티 (SRP)"""
    
    @staticmethod
    def escape_string(text: str) -> str:
        """문자열 이스케이프 처리"""
        if not text:
            return ""
        return (text
                .replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "\\r"))
    
    @staticmethod
    def format_properties(props: Dict[str, Any], alias: str = "n") -> str:
        """속성을 Cypher 형식으로 변환"""
        if not props:
            return f"{alias}.dummy = null"
        
        parts = []
        for k, v in props.items():
            if isinstance(v, bool):
                parts.append(f"{alias}.{k} = {str(v).lower()}")
            elif isinstance(v, (int, float)):
                parts.append(f"{alias}.{k} = {v}")
            else:
                escaped = CypherBuilder.escape_string(str(v))
                parts.append(f"{alias}.{k} = '{escaped}'")
        return ", ".join(parts)
    
    @staticmethod
    def create_node_query(node_data: Dict[str, Any]) -> str:
        """노드 생성 쿼리"""
        try:
            logger = Logger.get_logger("CypherBuilder")

            node_type = node_data['type']
            node_id = CypherBuilder.escape_string(node_data['id'])
            node_name = CypherBuilder.escape_string(node_data.get('name', '') or node_data['id'])
            props = node_data.get('properties', {})
            model_key = props.get('modelKey', '')
            props_str = CypherBuilder.format_properties(props, "n")

            query = f"""
MERGE (n:{node_type} {{id:'{node_id}', modelKey:'{model_key}'}})
SET n.name = '{node_name}',
    {props_str}
""".strip()

            logger.info(f"[CYPHER][NODE] Generated query for node type={node_type}, id={node_id}, name={node_name}, model_key={model_key}")
            logger.debug(f"[CYPHER][NODE] Full query: {query}")

            return query
        except Exception as e:
            logger = Logger.get_logger("CypherBuilder")
            logger.error(f"노드 쿼리 생성 실패: {e}")
            raise
    
    @staticmethod
    def create_relationship_query(rel_data: Dict[str, Any]) -> str:
        """관계 생성 쿼리"""
        try:
            logger = Logger.get_logger("CypherBuilder")

            # Log input data BEFORE processing
            logger.info(f"[CYPHER][REL][INPUT] Creating relationship query with rel_data: {rel_data}")

            source = CypherBuilder.escape_string(rel_data['source'])
            target = CypherBuilder.escape_string(rel_data['target'])
            rel_type = rel_data['type']
            props = rel_data.get('properties', {})
            model_key = props.get('modelKey', '')
            props_str = CypherBuilder.format_properties(props, "r")

            # Log processed values
            logger.info(f"[CYPHER][REL][PROCESSED] source={source}, target={target}, type={rel_type}, model_key={model_key}")

            query = f"""
WITH '{source}' AS sid, '{target}' AS tid, {('NULL' if model_key is None or model_key == '' else "'" + str(model_key) + "'")} AS mk
MATCH (a {{id: sid}}), (b {{id: tid}})
// Conditional modelKey constraints:
//  - If node has :bp label, ignore modelKey check
//  - Else require modelKey match only when mk IS NOT NULL
WHERE ( (a:bp) OR mk IS NULL OR a.modelKey = mk )
  AND ( (b:bp) OR mk IS NULL OR b.modelKey = mk )
WITH DISTINCT a, b
CREATE (a)-[r:{rel_type}]->(b)
SET {props_str}
""".strip()

            logger.info(f"[CYPHER][REL][OUTPUT] Generated relationship query for {source} -[{rel_type}]-> {target}")
            logger.debug(f"[CYPHER][REL][OUTPUT] Full query: {query}")

            return query
        except Exception as e:
            logger = Logger.get_logger("CypherBuilder")
            logger.error(f"관계 쿼리 생성 실패: {e}")
            raise

    @staticmethod
    def create_category_rel_query(rel_data: Dict[str, Any]) -> str:
        """카테고리 관계 생성 쿼리 (modelKey 검증 없음)"""
        try:
            logger = Logger.get_logger("CypherBuilder")

            # Log input data BEFORE processing
            logger.info(f"[CYPHER][CATEGORY_REL][INPUT] Creating category relationship query with rel_data: {rel_data}")

            source = CypherBuilder.escape_string(rel_data['source'])
            target = CypherBuilder.escape_string(rel_data['target'])
            rel_type = rel_data['type']
            props = rel_data.get('properties', {})
            props_str = CypherBuilder.format_properties(props, "r")

            # Log processed values
            logger.info(f"[CYPHER][CATEGORY_REL][PROCESSED] source={source}, target={target}, type={rel_type}")

            # Simple query without modelKey constraints
            query = f"""
MATCH (a {{id: '{source}'}}), (b {{id: '{target}'}})
CREATE (a)-[r:{rel_type}]->(b)
SET {props_str}
""".strip()

            logger.info(f"[CYPHER][CATEGORY_REL][OUTPUT] Generated category relationship query for {source} -[{rel_type}]-> {target}")
            logger.debug(f"[CYPHER][CATEGORY_REL][OUTPUT] Full query: {query}")

            return query
        except Exception as e:
            logger = Logger.get_logger("CypherBuilder")
            logger.error(f"카테고리 관계 쿼리 생성 실패: {e}")
            raise