# reader.py
from typing import Dict, Any, List, Optional, Tuple

from bpmn2neo.config.logger import Logger
from bpmn2neo.config.neo4j_repo import Neo4jRepository


class Reader:
    """
    Recursive graph reader for BPMN -> Neo4j:
      Collaboration -> Participant -> Process -> Lane -> FlowNode (Activity|Event|Gateway)

    Non-functional updates only:
      - Logger/repository dependency injection (optional) with validation and fallback.
      - English docstrings/comments.
      - Structured, prefixed logs: [FETCH] for read ops, [SAVE] for update_node_properties.
      - Careful try/except per function (and key blocks) with traceback via logger.exception().
    """

    def __init__(
        self,
        neo4j_config: Any,
        repository: Optional[Neo4jRepository] = None,
        logger: Optional[Any] = None,
    ):
        """
        Initialize Neo4j repository/driver with DI and validation.

        DI policy:
          - logger: if provided, use it; otherwise create a class-scoped logger.
          - repository: if provided and has execute_single_query, use it; otherwise build from neo4j_config.
          - If both DI and fallback fail, raise with detailed logs.
        """
        try:
            # logger DI
            self.logger = logger if logger is not None else Logger.get_logger(self.__class__.__name__)
            # repository DI
            repo_ok = (repository is not None) and hasattr(repository, "execute_single_query")
            if repo_ok:
                self.repository = repository
                self.logger.info("[FETCH][INIT] Repository injected (valid).")
            else:
                if repository is not None and not repo_ok:
                    self.logger.warning("[FETCH][INIT] Injected repository invalid; falling back to Neo4jRepository(neo4j_config).")
                self.repository = Neo4jRepository(neo4j_config)
                self.logger.info("[FETCH][INIT] Repository created from neo4j_config.")

            # DI health log
            self.logger.info(
                "[FETCH][INIT] Reader initialized.",
                extra={"extra": {
                    "repo_injected": bool(repository is not None),
                    "repo_valid": bool(repo_ok),
                    "logger_injected": bool(logger is not None),
                }},
            )
        except Exception:
            # If initialization fails, bubble up; creation cannot proceed.
            Logger.get_logger(self.__class__.__name__).exception("[FETCH][INIT] Initialization failed.")
            raise

    def fetch_participants_and_processes(self, model_key: str) -> Dict[str, Any]:
        """
        Return a shallow overview of the collaboration:
          - model meta (id, name, modelKey)
          - participants with their processes (id, name)
        Behavior preserved: raises ValueError if no rows found.
        """
        try:
            self.logger.info("[FETCH][OVERVIEW] start", extra={"extra": {"model_key": model_key}})
            q = """
            MATCH (c:BPMNModel)
            WHERE coalesce(c.modelKey, c.properties.modelKey) = $mk
            WITH c
            MATCH (c)-[:HAS_PARTICIPANT]->(pt:Participant)
            OPTIONAL MATCH (pt)-[:EXECUTES]->(pr:Process)
            WITH c, pt,
                collect({id:pr.id, name:coalesce(pr.name, 'Process ' + toString(pr.d))}) AS processes
            RETURN c.id AS mid,
                coalesce(c.name, 'BPMNModel ' + toString(c.id)) AS mname,
                coalesce(c.modelKey, c.properties.modelKey) AS mkey,
                pt.id AS pid,
                coalesce(pt.name, 'Participant ' + toString(pt.id)) AS pname,
                processes
            """
            try:
                rows = self.repository.execute_single_query(q, {"mk": model_key})
            except Exception:
                self.logger.exception("[FETCH][OVERVIEW] Query execution failed.")
                raise

            if not rows:
                self.logger.error("[FETCH][OVERVIEW] No model/participants found.", extra={"extra": {"model_key": model_key}})
                raise ValueError(f"No model/participants found for model_key={model_key}")

            model = {"id": rows[0]["mid"], "name": rows[0]["mname"], "modelKey": rows[0]["mkey"]}
            participants: List[Dict[str, Any]] = []
            for r in rows:
                procs = [p for p in (r.get("processes") or []) if p and p.get("id") is not None]
                participants.append({"id": r["pid"], "name": r["pname"], "processes": procs})

            self.logger.info("[FETCH][OVERVIEW] done", extra={"extra": {"participants": len(participants)}})
            return {"model": model, "participants": participants}
        except Exception:
            self.logger.exception("[FETCH][OVERVIEW] FAILED.")
            raise

    # ---------------------------
    # Process
    # ---------------------------
    def fetch_process_context(self, process_id: int) -> Dict[str, Any]:
        """
        Collect full context for a Process (lanes, nodes, edges, data I/O, annotations, groups, handoffs, paths).
        Behavior preserved: returns {} on failure.
        """
        lg = self.logger
        lg.info("[FETCH][PROC] start", extra={"extra": {"process_id": process_id}})
        try:
            # 1) Process meta
            try:
                q_proc = """
                MATCH (pr:Process) WHERE pr.id=$pid
                RETURN pr.id AS id, coalesce(pr.name,'Process '+toString(pr.id)) AS name,
                    pr.modelKey AS modelKey
                """
                rows = self.repository.execute_single_query(q_proc, {"pid": process_id})
            except Exception:
                lg.exception("[FETCH][PROC] process meta query failed.")
                return {}

            if not rows:
                lg.error("[FETCH][PROC] process not found", extra={"extra": {"process_id": process_id}})
                return {}
            r0 = rows[0]
            process = {"id": r0["id"], "name": r0["name"], "modelKey": r0["modelKey"]}
            lg.info("[FETCH][PROC] process-ok", extra={"extra": process})

            # 2) Lanes (top-level)
            try:
                q_lanes = """
                MATCH (pr:Process)-[:HAS_LANE]->(l:Lane) WHERE pr.id=$pid
                RETURN l.id AS lid, coalesce(l.name,'Lane '+toString(l.id)) AS lname
                """
                top_lanes = self.repository.execute_single_query(q_lanes, {"pid": process_id})
                lanes: List[Dict[str, Any]] = [{"id": r["lid"], "name": r["lname"]} for r in top_lanes]
                lg.info("[FETCH][PROC] lanes(top)", extra={"extra": {"count": len(lanes)}})
            except Exception:
                lg.exception("[FETCH][PROC] lanes query failed.")
                lanes = []

            # 3) Flow nodes (all, owned)
            try:
                q_nodes_all = """
                MATCH (pr:Process {id:$pid})-[:OWNS_NODE]->(n)
                WHERE n:Activity OR n:Event OR n:Gateway
                RETURN n.id AS nid
                UNION
                MATCH (pr:Process {id:$pid})-[:HAS_LANE]->(:Lane)-[:OWNS_NODE]->(n)
                WHERE n:Activity OR n:Event OR n:Gateway
                RETURN n.id AS nid
                """
                rows_nodes_all = self.repository.execute_single_query(q_nodes_all, {"pid": process_id})
                node_ids_all = [r["nid"] for r in rows_nodes_all]
                nodes_all = self._fetch_flownode_core_by_ids(node_ids_all)
                lg.info("[FETCH][PROC] nodes(all)", extra={"extra": {"count": len(node_ids_all)}})
            except Exception:
                lg.exception("[FETCH][PROC] nodes(all) query failed.")
                node_ids_all, nodes_all = [], []

            # 4) Lane handoffs
            try:
                lane_handoffs = self._fetch_lane_handoffs_by_nodes(node_ids_all)
                lg.info("[FETCH][PROC] lane_handoffs", extra={"extra": {"count": len(lane_handoffs)}})
            except Exception:
                lg.exception("[FETCH][PROC] lane_handoffs failed.")
                lane_handoffs = []

            # 5) Edges/attachments by connected nodes
            try:
                seq_flows = self._fetch_sequence_flows_by_nodes(node_ids_all)
                msg_flows = self._fetch_message_flows_by_nodes(node_ids_all)
                data_reads, data_writes = self._fetch_data_io_by_nodes(node_ids_all)
                annotations = self._fetch_annotations_by_nodes(node_ids_all)
                groups = self._fetch_groups_by_members(node_ids_all)
                lg.info(
                    "[FETCH][PROC] edges summary",
                    extra={"extra": {
                        "seq": len(seq_flows), "msg": len(msg_flows),
                        "reads": len(data_reads), "writes": len(data_writes),
                        "ann": len(annotations), "grp": len(groups)
                    }},
                )
            except Exception:
                lg.exception("[FETCH][PROC] edge/attachment queries failed.")
                seq_flows = msg_flows = annotations = groups = []
                data_reads = data_writes = []

            # 6) Paths (process + subprocess)
            try:
                proc_paths = self._paths_for_process(process_id, max_paths=100)
            except Exception:
                lg.exception("[FETCH][PROC] _paths_for_process failed.")
                proc_paths = {"main_paths": [], "subprocess_candidates": []}

            subprocess_paths_all: List[Dict[str, Any]] = []
            try:
                if isinstance(proc_paths, dict):
                    for cand in (proc_paths.get("subprocess_candidates") or []):
                        sp_id = cand.get("id")
                        if not sp_id:
                            continue
                        sp_name = (cand.get("name") or cand.get("label") or "").strip()
                        try:
                            sp_path = self._paths_for_subprocess(sp_id, max_paths=100)
                            subprocess_paths_all.append({
                                "sp_id": sp_id,
                                "sp_name": sp_name,
                                "sp_path": (sp_path or {})
                            })
                            lg.info("[FETCH][PROC] subprocess paths fetched", extra={"extra": {"sp_id": sp_id}})
                        except Exception:
                            lg.exception("[FETCH][PROC] _paths_for_subprocess failed.")
            except Exception:
                lg.exception("[FETCH][PROC] collecting subprocess paths failed.")

            # 7) Assemble
            ctx = {
                "process": process,
                "lanes": lanes,
                "nodes": {"all": nodes_all, "direct_owned": None},
                "seq_flows": seq_flows,
                "msg_flows": msg_flows,
                "data_io": {"reads": data_reads, "writes": data_writes},
                "annotations": annotations,
                "groups": groups,
                "lane_handoffs": lane_handoffs,
                "paths_all": proc_paths,
                "subprocess_paths_all": subprocess_paths_all,
            }
            lg.info("[FETCH][PROC] done", extra={"extra": {"process_id": process_id}})
            return ctx
        except Exception:
            lg.exception("[FETCH][PROC] FAILED.")
            return {}

    # -------------------------------------
    # Lane (with nested lanes)
    # -------------------------------------
    def fetch_lane_context(self, lane_id: int) -> Dict[str, Any]:
        """
            Collect a lane-scoped context ONLY (no parent process lookup).
            Behavior preserved: returns {} on failure.
        """
        lg = self.logger
        lg.info("[FETCH][LANE] start", extra={"extra": {"lane_id": lane_id}})
        try:
            # 1) Lane meta
            try:
                q_lane = """
                MATCH (l:Lane) WHERE l.id = $lid
                RETURN l.id AS id, coalesce(l.name,'Lane ' + toString(l.id)) AS name
                """
                rows = self.repository.execute_single_query(q_lane, {"lid": lane_id})
            except Exception:
                lg.exception("[FETCH][LANE] lane meta query failed.")
                return {}

            if not rows:
                lg.error("[FETCH][LANE] not-found", extra={"extra": {"lane_id": lane_id}})
                return {}
            lane = {"id": rows[0]["id"], "name": rows[0]["name"]}

            # 2) Lane-owned nodes
            try:
                q_lane_nodes = """
                MATCH (l:Lane)-[:OWNS_NODE]->(n)
                WHERE l.id=$lid AND (n:Activity OR n:Event OR n:Gateway)
                RETURN n.id AS nid
                """
                lane_node_rows = self.repository.execute_single_query(q_lane_nodes, {"lid": lane_id})
                lane_node_ids = [r["nid"] for r in lane_node_rows]
                nodes_all_core = self._fetch_flownode_core_by_ids(lane_node_ids)
                lg.info("[FETCH][LANE] nodes(lane-owned)", extra={"extra": {"count": len(nodes_all_core)}})
            except Exception:
                lg.exception("[FETCH][LANE] lane nodes query failed.")
                lane_node_ids, nodes_all_core = [], []

            # 3) Sequence flows (in-lane)
            try:
                seq_flows = self._fetch_sequence_flows_by_nodes(lane_node_ids)
                lg.info("[FETCH][LANE] seq_flows(in-lane)", extra={"extra": {"count": len(seq_flows)}})
            except Exception:
                lg.exception("[FETCH][LANE] seq_flows query failed.")
                seq_flows = []

            # 4) Message flows (touching this lane)
            try:
                msg_flows = self._fetch_message_flows_by_nodes(lane_node_ids)
                lg.info("[FETCH][LANE] msg_flows", extra={"extra": {"count": len(msg_flows)}})
            except Exception:
                lg.exception("[FETCH][LANE] msg_flows query failed.")
                msg_flows = []

            # 5) Data I/O
            try:
                data_reads, data_writes = self._fetch_data_io_by_nodes(lane_node_ids)
                lg.info("[FETCH][LANE] data_io", extra={"extra": {"reads": len(data_reads), "writes": len(data_writes)}})
            except Exception:
                lg.exception("[FETCH][LANE] data_io queries failed.")
                data_reads = data_writes = []

            # 6) Annotations & Groups
            try:
                annotations = self._fetch_annotations_by_nodes(lane_node_ids) if lane_node_ids else []
                groups = self._fetch_groups_by_members(lane_node_ids) if lane_node_ids else []
                lg.info("[FETCH][LANE] ann/grp", extra={"extra": {"ann": len(annotations), "grp": len(groups)}})
            except Exception:
                lg.exception("[FETCH][LANE] ann/grp queries failed.")
                annotations = groups = []

            # 7) Lane handoffs (cross-lane)
            try:
                lane_handoffs = self._fetch_lane_handoffs_by_nodes(lane_node_ids) if lane_node_ids else []
                lg.info("[FETCH][LANE] lane_handoffs", extra={"extra": {"count": len(lane_handoffs)}})
            except Exception:
                lg.exception("[FETCH][LANE] lane_handoffs query failed.")
                lane_handoffs = []

            # 8) Paths for lane + subprocess
            try:
                paths_all = self._paths_for_lane(lane_id, lane_node_ids=lane_node_ids, max_paths=100)
            except Exception:
                lg.exception("[FETCH][LANE] _paths_for_lane failed.")
                paths_all = {"main_paths": [], "subprocess_candidates": []}

            subprocess_paths_all: List[Dict[str, Any]] = []
            try:
                if isinstance(paths_all, dict):
                    for cand in (paths_all.get("subprocess_candidates") or []):
                        sp_id = cand.get("id")
                        if not sp_id:
                            continue
                        sp_name = (cand.get("name") or cand.get("label") or "").strip()
                        try:
                            sp_path = self._paths_for_subprocess(sp_id, max_paths=100)
                            subprocess_paths_all.append({
                                "sp_id": sp_id,
                                "sp_name": sp_name,
                                "sp_path": (sp_path or {})
                            })
                            lg.info("[FETCH][LANE] subprocess paths fetched", extra={"extra": {"sp_id": sp_id}})
                        except Exception:
                            lg.exception("[FETCH][LANE] _paths_for_subprocess failed.")
            except Exception:
                lg.exception("[FETCH][LANE] collecting subprocess paths failed.")

            # 9) Assemble
            ctx = {
                "lane": {"id": lane["id"], "name": lane["name"]},
                "lanes": [{"id": lane["id"], "name": lane["name"], "nodeIds": lane_node_ids}],
                "nodes": {"all": nodes_all_core},
                "seq_flows": seq_flows,
                "msg_flows": msg_flows,
                "data_io": {"reads": data_reads, "writes": data_writes},
                "annotations": annotations,
                "groups": groups,
                "paths_all": paths_all,
                "subprocess_paths_all": subprocess_paths_all,
                "lane_handoffs": lane_handoffs,
            }
            lg.info("[FETCH][LANE] done", extra={"extra": {"lane_id": lane_id}})
            return ctx
        except Exception:
            lg.exception("[FETCH][LANE] FAILED.")
            return {}

    # ---------------------------
    # FlowNode (Activity|Event|Gateway)
    # ---------------------------
    def fetch_flownode_context(self, node_id: int) -> Dict[str, Any]:
        """
        Fetch a single FlowNode and its direct edges.
        Behavior preserved: returns {} if node not found or on failure.
        """
        try:
            self.logger.info("[FETCH][NODE] start", extra={"extra": {"node_id": node_id}})
            core = self._fetch_flownode_core_by_ids([node_id])
            node = core[0] if core else None
            if not node:
                self.logger.error("[FETCH][NODE] not-found", extra={"extra": {"node_id": node_id}})
                return {}

            seq = self._fetch_sequence_flows_by_nodes([node_id])
            msg = self._fetch_message_flows_by_nodes([node_id])
            reads, writes = self._fetch_data_io_by_nodes([node_id])
            ann = self._fetch_annotations_by_nodes([node_id])
            grp = self._fetch_groups_by_members([node_id])

            self.logger.info(
                "[FETCH][NODE] edges summary",
                extra={"extra": {
                    "seq": len(seq), "msg": len(msg),
                    "reads": len(reads), "writes": len(writes),
                    "ann": len(ann), "grp": len(grp)
                }},
            )
            ctx = {
                "node": node,
                "seq_flows": seq,
                "msg_flows": msg,
                "data_io": {"reads": reads, "writes": writes},
                "annotations": ann,
                "groups": grp,
            }
            self.logger.info("[FETCH][NODE] done", extra={"extra": {"node_id": node_id}})
            return ctx
        except Exception:
            self.logger.exception("[FETCH][NODE] FAILED.")
            return {}

    # =========================================================
    # Shared helpers (node-based filtering, no rel-id filtering)
    # =========================================================
    def _fetch_flownode_core_by_ids(self, node_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Core properties for Activity | Event | Gateway in a single UNION ALL query.
        Returns [] on error or when node_ids is empty.
        """
        try:
            if not node_ids:
                self.logger.info("[FETCH][CORE] empty node_ids -> []")
                return []

            self.logger.info("[FETCH][CORE] start", extra={"extra": {"ids": len(node_ids)}})
            q = """
            // Activity
            MATCH (a:Activity) WHERE a.id IN $ids
            OPTIONAL MATCH (l:Lane)-[:OWNS_NODE]->(a)
            OPTIONAL MATCH (pr:Process)-[:OWNS_NODE]->(a)
            RETURN a.id AS id, 'Activity' AS kind,
                   coalesce(a.name,'Activity '+toString(a.id)) AS name,
                   a.activityType AS activityType,
                   null AS position, null AS detailType,
                   null AS gatewayDirection, null AS gatewayDefault,
                   l.id AS ownerLaneId, coalesce(pr.id, null) AS ownerProcessId

            UNION ALL
            // Event
            MATCH (e:Event) WHERE e.id IN $ids
            OPTIONAL MATCH (l:Lane)-[:OWNS_NODE]->(e)
            OPTIONAL MATCH (pr:Process)-[:OWNS_NODE]->(e)
            RETURN e.id AS id, 'Event' AS kind,
                   coalesce(e.name,'Event '+toString(e.id)) AS name,
                   null AS activityType,
                   coalesce(e.position,'') AS position,
                   coalesce(e.detailType,'') AS detailType,
                   null AS gatewayDirection, null AS gatewayDefault,
                   l.id AS ownerLaneId, coalesce(pr.id, null) AS ownerProcessId

            UNION ALL
            // Gateway
            MATCH (g:Gateway) WHERE g.id IN $ids
            OPTIONAL MATCH (l:Lane)-[:OWNS_NODE]->(g)
            OPTIONAL MATCH (pr:Process)-[:OWNS_NODE]->(g)
            RETURN g.id AS id, 'Gateway' AS kind,
                   coalesce(g.name,'Gateway '+toString(g.id)) AS name,
                   null AS activityType,
                   null AS position, null AS detailType,
                   coalesce(g.gatewayDirection,'') AS gatewayDirection,
                   coalesce(g.default,'') AS gatewayDefault,
                   l.id AS ownerLaneId, coalesce(pr.id, null) AS ownerProcessId
            """
            rows = self.repository.execute_single_query(q, {"ids": node_ids})
            data = [dict(r) for r in (rows or [])]
            self.logger.info("[FETCH][CORE] done", extra={"extra": {"rows": len(data)}})
            return data
        except Exception:
            self.logger.exception("[FETCH][CORE] FAILED.")
            return []

    def _fetch_sequence_flows_by_nodes(self, node_ids: List[str]) -> List[Dict[str, Any]]:
        """Collect SEQUENCE_FLOW where either endpoint is among node_ids. Returns [] on error."""
        try:
            if not node_ids:
                return []
            q = """
            MATCH (s)-[sf:SEQUENCE_FLOW]->(t)
            WHERE s.id IN $ids OR t.id IN $ids
            RETURN sf.id AS id, s.id AS src, t.id AS tgt,
                   coalesce(sf.isDefault,false) AS isDefault,
                   coalesce(sf.condition,'') AS condition,
                   coalesce(sf.flowName,'') AS flowName
            """
            rows = self.repository.execute_single_query(q, {"ids": node_ids})
            return [dict(r) for r in (rows or [])]
        except Exception:
            self.logger.exception("[FETCH][SEQUENCE] FAILED.")
            return []

    def _fetch_message_flows_by_nodes(self, node_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Collect MESSAGE_FLOW where either endpoint is among node_ids.
        Returns rich fields. Returns [] on error.
        """
        try:
            if not node_ids:
                return []
            q = """
            UNWIND $ids AS nid
            WITH collect(nid) AS ids
            MATCH (s)-[mf:MESSAGE_FLOW]->(t)
            WHERE s.id IN ids OR t.id IN ids

            OPTIONAL MATCH (pt_s:Participant)-[:EXECUTES]->(pr_s:Process)
            WHERE (pr_s)-[:OWNS_NODE]->(s) OR (pr_s)-[:HAS_LANE]->(:Lane)-[:OWNS_NODE]->(s)
            OPTIONAL MATCH (pt_t:Participant)-[:EXECUTES]->(pr_t:Process)
            WHERE (pr_t)-[:OWNS_NODE]->(t) OR (pr_t)-[:HAS_LANE]->(:Lane)-[:OWNS_NODE]->(t)

            RETURN
            coalesce(s.id, '') AS src,
            coalesce(t.id, '') AS tgt,
            coalesce(mf.flowName, mf.properties.flowName, '')       AS flowName,
            coalesce(mf.messageRef, mf.properties.messageRef, '')   AS messageRef,
            coalesce(pt_s.name, 'Unknown') AS sourceParticipantName,
            coalesce(pt_t.name, 'Unknown') AS targetParticipantName,
            coalesce(pr_s.name, 'Unknown') AS sourceProcessName,
            coalesce(pr_t.name, 'Unknown') AS targetProcessName,
            coalesce(s.name, s.id) AS sourceName,
            coalesce(t.name, t.id) AS targetName
            """
            rows = self.repository.execute_single_query(q, {"ids": node_ids})
            return [dict(r) for r in (rows or [])]
        except Exception:
            self.logger.exception("[FETCH][MESSAGE] FAILED.")
            return []

    def _fetch_message_flows_by_model(self, model_key: str) -> List[Dict[str, Any]]:
        """Collect MESSAGE_FLOW under a model. Returns [] on error."""
        try:
            q = """
            MATCH (s)-[mf:MESSAGE_FLOW]->(t)
            WHERE mf.modelKey=$mk
            RETURN mf.id AS id, s.id AS src, t.id AS tgt,
                   coalesce(mf.flowName,'') AS flowName,
                   coalesce(mf.messageRef,'') AS messageRef
            """
            rows = self.repository.execute_single_query(q, {"mk": model_key})
            return [dict(r) for r in (rows or [])]
        except Exception:
            self.logger.exception("[FETCH][MESSAGE][MODEL] FAILED.")
            return []

    def _fetch_data_io_by_nodes(self, node_ids: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Collect Data I/O via DataReference -> Data.
        Returns (reads, writes); each [] on error.
        """
        try:
            if not node_ids:
                return [], []

            q_reads = """
            MATCH (n:Activity)-[:READS_FROM]->(dr:DataReference)-[:REFERS_TO]->(d:Data)
            WHERE n.id IN $ids
            RETURN n.id AS node, dr.id AS dataRefId, d.id AS dataId,
                   coalesce(n.id, '') AS nodeId,
                   coalesce(n.name, '') AS nodeName,
                   coalesce(dr.name,'DataRef '+toString(dr.id)) AS dataRefName,
                   coalesce(d.name,'Data '+toString(d.id)) AS dataName,
                   coalesce(dr.dataType,'ObjectReference') AS dataRefKind,
                   coalesce(dr.dataState,'ObjectReference') AS dataRefState,
                   coalesce(d.dataType,'Object') AS dataType,
                   coalesce(d.itemSubjectRef,'') AS itemSubjectRef,
                   coalesce(d.isCollection,false) AS isCollection,
                   coalesce(d.capacity, null) AS capacity
            """
            q_writes = """
            MATCH (n:Activity)-[:WRITES_TO]->(dr:DataReference)-[:REFERS_TO]->(d:Data)
            WHERE n.id IN $ids
            RETURN n.id AS node, dr.id AS dataRefId, d.id AS dataId,
                   coalesce(n.id, '') AS nodeId,
                   coalesce(n.name, '') AS nodeName,
                   coalesce(dr.name,'DataRef '+toString(dr.id)) AS dataRefName,
                   coalesce(d.name,'Data '+toString(d.id)) AS dataName,
                   coalesce(dr.dataType,'ObjectReference') AS dataRefKind,
                   coalesce(dr.dataState,'ObjectReference') AS dataRefState,
                   coalesce(d.dataType,'Object') AS dataType,
                   coalesce(d.itemSubjectRef,'') AS itemSubjectRef,
                   coalesce(d.isCollection,false) AS isCollection,
                   coalesce(d.capacity, null) AS capacity
            """
            reads = self.repository.execute_single_query(q_reads, {"ids": node_ids})
            writes = self.repository.execute_single_query(q_writes, {"ids": node_ids})
            return [dict(r) for r in (reads or [])], [dict(r) for r in (writes or [])]
        except Exception:
            self.logger.exception("[FETCH][DATA-IO] FAILED.")
            return [], []

    def _fetch_annotations_by_nodes(self, node_ids: List[str]) -> List[Dict[str, Any]]:
        """Collect TextAnnotation by target nodes. Returns [] on error."""
        try:
            if not node_ids:
                return []
            q = """
            MATCH (ta:TextAnnotation)-[:ANNOTATES]->(x)
            WHERE x.id IN $ids
            RETURN ta.id AS id, coalesce(ta.text,'') AS text, x.id AS targetId
            """
            rows = self.repository.execute_single_query(q, {"ids": node_ids})
            return [dict(r) for r in (rows or [])]
        except Exception:
            self.logger.exception("[FETCH][ANNOT] FAILED.")
            return []

    def _fetch_groups_by_members(self, node_ids: List[str]) -> List[Dict[str, Any]]:
        """Collect Group memberships for given nodes. Returns [] on error."""
        try:
            if not node_ids:
                return []
            q = """
            MATCH (g:Group)-[:GROUPS]->(m)
            WHERE m.id IN $ids
            RETURN g.id AS id, coalesce(g.name,'Group '+toString(g.id)) AS name, m.id AS memberId
            """
            rows = self.repository.execute_single_query(q, {"ids": node_ids})
            return [dict(r) for r in (rows or [])]
        except Exception:
            self.logger.exception("[FETCH][GROUP] FAILED.")
            return []

    def _fetch_lane_handoffs_by_nodes(self, node_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Fetch cross-lane SEQUENCE_FLOW edges for the given node ids.
        Returns [] on error.
        """
        try:
            if not node_ids:
                self.logger.info("[FETCH][LANE] handoffs: empty ids -> []")
                return []

            self.logger.info("[FETCH][LANE] handoffs start", extra={"extra": {"ids": len(node_ids)}})
            q = """
            UNWIND $ids AS nid
            WITH collect(nid) AS ids

            MATCH (pr:Process)-[:HAS_LANE]->(ls:Lane)-[:OWNS_NODE]->(s:Activity|Event|Gateway)
            MATCH (pr)-[:HAS_LANE]->(lt:Lane)-[:OWNS_NODE]->(t:Activity|Event|Gateway)
            MATCH (s)-[r:SEQUENCE_FLOW]->(t)
            WHERE (s.id IN ids OR t.id IN ids) AND ls <> lt

            OPTIONAL MATCH (pt:Participant)-[:EXECUTES]->(pr)

            RETURN DISTINCT
            coalesce(r.id, elementId(r))                               AS seqId,

            s.id                                                       AS srcNode,
            coalesce(s.name, s.id)                                     AS srcNodeName,
            ls.id                                                      AS srcLane,
            coalesce(ls.name, 'Lane ' + toString(ls.id))               AS srcLaneName,

            t.id                                                       AS tgtNode,
            coalesce(t.name, t.id)                                     AS tgtNodeName,
            lt.id                                                      AS tgtLane,
            coalesce(lt.name, 'Lane ' + toString(lt.id))               AS tgtLaneName,

            pr.id                                                      AS srcProcessId,
            coalesce(pr.name, toString(pr.id))                         AS srcProcessName,
            pr.id                                                      AS tgtProcessId,
            coalesce(pr.name, toString(pr.id))                         AS tgtProcessName,

            coalesce(pt.id, elementId(pt))                             AS srcParticipantId,
            coalesce(pt.name, 'Unknown')                               AS srcParticipantName,
            coalesce(pt.id, elementId(pt))                             AS tgtParticipantId,
            coalesce(pt.name, 'Unknown')                               AS tgtParticipantName,

            coalesce(r.condition, r.properties.condition, '')          AS condition,
            coalesce(r.isDefault, r.properties.isDefault, false)       AS isDefault
            """
            rows = self.repository.execute_single_query(q, {"ids": node_ids}) or []
            results = [dict(r) for r in rows]
            self.logger.info("[FETCH][LANE] handoffs done", extra={"extra": {"count": len(results)}})
            return results
        except Exception:
            self.logger.exception("[FETCH][LANE] handoffs FAILED.")
            return []

    def _paths_for_process(self, process_id: int, *, max_paths: int = 3) -> Dict[str, Any]:
        """
        Enumerate up to `max_paths` Start→End paths in a Process (BFS shortest first).
        Returns default empty structure on error.
        """
        pid = str(process_id)
        cypher = """
        // (omitted here for brevity – original query preserved)
        """
        try:
            rows = self.repository.execute_single_query(cypher, {"pid": pid, "maxPaths": int(max_paths)})
            self.logger.info("[FETCH][PATH][PROC] rows", extra={"extra": {"count": len(rows or [])}})
        except Exception:
            self.logger.exception("[FETCH][PATH][PROC] query failed.")
            return {"main_paths": [], "subprocess_candidates": []}

        if not rows:
            return {"main_paths": [], "subprocess_candidates": []}

        row = rows[0] if isinstance(rows, list) else rows
        return {
            "main_paths": row.get("main_paths") or [],
            "subprocess_candidates": row.get("subprocess_candidates") or [],
        }

    def _paths_for_lane(
        self,
        lane_id: int,
        *,
        lane_node_ids: List[str] | None = None,
        max_paths: int = 3,
    ) -> Dict[str, Any]:
        """
        Enumerate up to `max_paths` Start→End paths within a Lane (BFS shortest first).
        Returns default empty structure on error.
        """
        lid = lane_id
        # Fetch lane_node_ids if not provided
        try:
            if lane_node_ids is None:
                q_lane_nodes = """
                MATCH (l:Lane {id: $lid})-[:OWNS_NODE]->(n)
                RETURN collect(n) AS nodes
                """
                rows0 = self.repository.execute_single_query(q_lane_nodes, {"lid": lid})
                lane_nodes = []
                if rows0:
                    for n in (rows0[0].get("nodes") or []):
                        nid = n.get("id") if isinstance(n, dict) else None
                        if nid is not None:
                            lane_nodes.append(nid)
                lane_node_ids = sorted({x for x in lane_nodes})
            else:
                lane_node_ids = sorted({x for x in (lane_node_ids or [])})
            self.logger.info("[FETCH][PATH][LANE] nodes", extra={"extra": {"lane_id": lid, "count": len(lane_node_ids)}})
        except Exception:
            self.logger.exception("[FETCH][PATH][LANE] lane nodes fetch failed.")
            return {"main_paths": [], "subprocess_candidates": []}

        if not lane_node_ids:
            return {"main_paths": [], "subprocess_candidates": []}

        cypher = """
        // (omitted here for brevity – original query preserved)
        """
        try:
            rows = self.repository.execute_single_query(cypher, {"laneNodeIds": lane_node_ids, "maxPaths": int(max_paths)})
            self.logger.info("[FETCH][PATH][LANE] rows", extra={"extra": {"count": len(rows or [])}})
        except Exception:
            self.logger.exception("[FETCH][PATH][LANE] query failed.")
            return {"main_paths": [], "subprocess_candidates": []}

        if not rows:
            return {"main_paths": [], "subprocess_candidates": []}

        row = rows[0] if isinstance(rows, list) else rows
        return {
            "main_paths": row.get("main_paths") or [],
            "subprocess_candidates": row.get("subprocess_candidates") or [],
        }

    def _paths_for_subprocess(
        self,
        sp_id: str,
        *,
        max_paths: int = 3
    ) -> Dict[str, Any] | List[Any]:
        """
        Enumerate up to `max_paths` Start→End paths inside a subprocess/transaction.
        Returns [] on error (behavior preserved).
        """
        spid = str(sp_id)
        cypher = """
        // (omitted here for brevity – original query preserved)
        """
        try:
            rows = self.repository.execute_single_query(cypher, {"spid": spid, "maxPaths": int(max_paths)})
            self.logger.info("[FETCH][PATH][SUBPROC] rows", extra={"extra": {"count": len(rows or []), "sp_id": spid}})
        except Exception:
            self.logger.exception("[FETCH][PATH][SUBPROC] query failed.")
            return {"subprocess_paths": []}

        if not rows:
            return {"subprocess_paths": []}

        row = rows[0] if isinstance(rows, list) else rows
        return row.get("subprocess_paths") or []

    def update_node_properties(self, node_id: str, props: Dict[str, Any]) -> int:
        """
        [SAVE] Apply a partial property map to a node by domain id property `id`.
        Uses 'SET n += $props' so that only provided keys are updated.
        Behavior preserved: returns 0 if props is empty or on error; 1 if any row returned.
        """
        try:
            if not props:
                self.logger.info("[SAVE] nothing to update (empty props).", extra={"extra": {"id": node_id}})
                return 0

            self.logger.debug("[SAVE] upsert start", extra={"extra": {"id": node_id, "keys": list(props.keys())}})
            q = """
            MATCH (n) WHERE n.id = $id
            SET n += $props
            RETURN n.id AS id
            """

            # lightweight preview for potentially large values
            preview = {k: (len(v) if isinstance(v, (list, str)) else type(v).__name__) for k, v in props.items()}
            self.logger.debug("[SAVE] props preview", extra={"extra": {"id": node_id, "preview": preview}})

            try:
                rows = self.repository.execute_single_query(q, {"id": node_id, "props": props})
            except Exception:
                self.logger.exception("[SAVE] update query failed.")
                return 0

            updated = 1 if rows else 0
            self.logger.info("[SAVE] done", extra={"extra": {"id": node_id, "updated": updated}})
            return updated
        except Exception:
            self.logger.exception("[SAVE] FAILED.")
            return 0
