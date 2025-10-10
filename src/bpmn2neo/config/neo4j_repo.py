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

    def execute_single_query(self, query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Execute a single Cypher query and return list of dict records."""
        params = params or {}
        self.logger.info("[CYPHER][SINGLE] running", extra={"extra": {"q": self._short(query), "params": self._safe(params)}})
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
        node_type = node_data['type']
        node_id = CypherBuilder.escape_string(node_data['id'])
        node_name = CypherBuilder.escape_string(node_data.get('name', '') or node_data['id'])
        props = node_data.get('properties', {})
        props_str = CypherBuilder.format_properties(props, "n")
        
        return f"""
MERGE (n:{node_type} {{id:'{node_id}'}})
SET n.name = '{node_name}',
    {props_str}
""".strip()
    
    @staticmethod
    def create_relationship_query(rel_data: Dict[str, Any]) -> str:
        """관계 생성 쿼리"""
        source = CypherBuilder.escape_string(rel_data['source'])
        target = CypherBuilder.escape_string(rel_data['target'])
        rel_type = rel_data['type']
        props = rel_data.get('properties', {})
        props_str = CypherBuilder.format_properties(props, "r")
        
        return f"""
MATCH (a {{id:'{source}'}}), (b {{id:'{target}'}})
CREATE (a)-[r:{rel_type}]->(b)
SET {props_str}
""".strip()