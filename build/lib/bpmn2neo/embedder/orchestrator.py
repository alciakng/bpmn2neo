
# orchestrator.py (refactored to fetch all contexts and pass them into Builder)
from typing import Dict, Any, List, Set, Optional

from bpmn2neo.config.logger import Logger
from bpmn2neo.settings import Settings
from bpmn2neo.embedder.builder import Builder
from bpmn2neo.embedder.embedder import Embedder
from bpmn2neo.embedder.reader import Reader

LOG_PREFIX = "[Orchestration]"  # common structured prefix for this orchestrator

class Orchestrator:
    """
    Orchestrates end-to-end text building and persistence for a BPMN model.
    Fetches ALL contexts from Reader and passes them into Builder (which never fetches).

    Non-functional updates only:
      - English comments/logs
      - Block-level try/except with structured logging
      - Dependency injection validation and safe logger injection into Reader
    """

    def __init__(self, settings: Settings, logger: Optional[Any] = None):
        """
        Args:
            settings: BPMNEmbeddingSettings (lean bundle with neo4j/openai/pipeline)

        DI policy:
          - logger: if provided, use it; else create class-scoped logger.
          - Validate settings carries required sub-configs (neo4j, openai).
          - Reader/Embedder/Builder constructed from settings; Reader receives logger injection.
        """
        # ---- init-level try/except for robust boot ----
        try:
            self.logger = logger if logger is not None else Logger.get_logger(self.__class__.__name__)
            self.logger.info(f"{LOG_PREFIX}[INIT] start")

            # settings validation
            try:
                if not hasattr(settings, "neo4j"):
                    raise TypeError("Missing 'neo4j' in settings.")
                if not hasattr(settings, "openai"):
                    raise TypeError("Missing 'openai' in settings.")
            except Exception as e:
                # log and re-raise to avoid half-initialized orchestrator
                self.logger.exception(f"{LOG_PREFIX}[INIT] settings validation failed: {e}")
                raise

            self.settings = settings

            # Reader (Neo4j) with logger injection if supported
            try:
                # Reader may accept logger kwarg; if not, fallback to positional init
                try:
                    self.reader = Reader(settings.neo4j, logger=self.logger)  # preferred
                except TypeError:
                    # fallback for older signature (no logger param)
                    self.reader = Reader(settings.neo4j)
                    self.logger.warning(f"{LOG_PREFIX}[INIT] Reader does not accept logger injection; used fallback.")
                self.logger.info(f"{LOG_PREFIX}[INIT] Reader ready")
            except Exception as e:
                self.logger.exception(f"{LOG_PREFIX}[INIT] Reader init failed: {e}")
                raise

            # Embedder (OpenAI)
            try:
                self.embedder = Embedder(settings.openai)
                self.logger.info(f"{LOG_PREFIX}[INIT] Embedder ready")
            except Exception as e:
                self.logger.exception(f"{LOG_PREFIX}[INIT] Embedder init failed: {e}")
                raise

            # Builder (internal)
            try:
                self.builder = Builder(settings.neo4j, settings.openai, self.embedder)
                self.logger.info(f"{LOG_PREFIX}[INIT] Builder ready")
            except Exception as e:
                self.logger.exception(f"{LOG_PREFIX}[INIT] Builder init failed: {e}")
                raise

            self.logger.info(f"{LOG_PREFIX}[INIT] done")
        except Exception:
            # final catch to ensure caller gets a clear failure
            Logger.get_logger(self.__class__.__name__).exception(f"{LOG_PREFIX}[INIT] FAILED")
            raise

    def run_all(self, model_key: str) -> Dict[str, Any]:
        """
        Bottom-up pipeline:
        FlowNodes → Lanes → Process → Participant → Model
        Reader must provide paths_all; Builder never fetches.
        Returns a summary (counts/errors). Non-functional changes only.
        """
        self.logger.info(f"{LOG_PREFIX}[OVERVIEW] start", extra={"extra": {"model_key": model_key}})

        summary = {
            "model_key": model_key,
            "counts": {"model": 0, "participants": 0, "processes": 0, "lanes": 0, "flownodes": 0},
            "errors": [],
        }

        # 0) model + participants + their process lists
        try:
            overview = self.reader.fetch_participants_and_processes(model_key)
            participants = overview.get("participants") or []
            model_meta = overview.get("model") or {}

            self.logger.info(
                f"{LOG_PREFIX}[OVERVIEW] fetched",
                extra={"extra": {
                    "participants": len(participants),
                    "model_id": model_meta.get("id"),
                    "model_name": model_meta.get("name"),
                }},
            )
        except Exception as e:
            self._log_error(summary, f"{LOG_PREFIX}[OVERVIEW] fetch_participants_and_processes failed: {e}")
            self.logger.error(f"{LOG_PREFIX}[OVERVIEW] aborting pipeline due to overview failure")
            return summary

        # Build participant→processIds map and (optional) process name index
        try:
            proc_ids_by_part: Dict[str, List[str]] = {}
            proc_name_index: Dict[str, str] = {}
            for p in participants:
                pid = p.get("id")
                if pid is None:
                    continue
                plist = p.get("processes") or []
                ids: List[str] = []
                for pr in plist:
                    pr_id = pr.get("id")
                    pr_name = pr.get("name")
                    if pr_id is not None:
                        ids.append(pr_id)
                        if pr_name:
                            proc_name_index[pr_id] = pr_name
                proc_ids_by_part[pid] = ids

            all_process_ids: List[str] = sorted({i for ids in proc_ids_by_part.values() for i in ids})
            self.logger.info(
                f"{LOG_PREFIX}[OVERVIEW] index built",
                extra={"extra": {"unique_processes": len(all_process_ids)}},
            )
        except Exception as e:
            self._log_error(summary, f"{LOG_PREFIX}[OVERVIEW] build index failed: {e}")

        # Artifact stores
        process_artifacts: Dict[str, Dict[str, Any]] = {}
        lane_artifacts: Dict[str, Dict[str, Any]] = {}
        flownode_artifacts: Dict[str, Dict[str, Any]] = {}
        participant_artifacts: List[Dict[str, Any]] = []

        # ─────────────────────────────────────────────────────────────
        # 1) FlowNodes → 2) Lanes → 3) Process  (per process bottom-up)
        # ─────────────────────────────────────────────────────────────
        for proc_id in (all_process_ids or []):
            # Fetch process context
            try:
                self.logger.info(f"{LOG_PREFIX}[PROCESS] start", extra={"extra": {"process_id": proc_id}})
                p_ctx = self.reader.fetch_process_context(proc_id)
            except Exception as e:
                self._log_error(summary, f"{LOG_PREFIX}[PROCESS] fetch_process_context({proc_id}) failed: {e}")
                continue

            # 1) FlowNodes
            try:
                node_list = (p_ctx.get("nodes") or {}).get("all") or []
                self.logger.info(
                    f"{LOG_PREFIX}[NODE] plan",
                    extra={"extra": {"process_id": proc_id, "node_count": len(node_list)}},
                )
                for n in node_list:
                    nid = n.get("id")
                    if nid is None:
                        continue
                    try:
                        self.logger.info(f"{LOG_PREFIX}[NODE] start", extra={"extra": {"node_id": nid}})
                        n_ctx = self.reader.fetch_flownode_context(nid)
                        n_art = self.builder.build_flownode_texts(
                            node_ctx=n_ctx,
                            process_ctx=p_ctx,
                            compute_vector=True,
                            persist=False,
                        )
                        self.save_texts_and_vectors([n_art])
                        flownode_artifacts[nid] = n_art
                        summary["counts"]["flownodes"] += 1
                        self.logger.info(f"{LOG_PREFIX}[NODE] done", extra={"extra": {"node_id": nid}})
                    except Exception as e:
                        self._log_error(summary, f"{LOG_PREFIX}[NODE] node({nid}) failed: {e}")
            except Exception as e:
                self._log_error(summary, f"{LOG_PREFIX}[NODE] batch in process({proc_id}) failed: {e}")

            # 2) Lanes
            try:
                lanes = p_ctx.get("lanes") or []
                self.logger.info(
                    f"{LOG_PREFIX}[LANE] plan",
                    extra={"extra": {"process_id": proc_id, "lane_count": len(lanes)}},
                )
                for ln in lanes:
                    lid = ln.get("id")
                    if lid is None:
                        continue
                    try:
                        self.logger.info(f"{LOG_PREFIX}[LANE] start", extra={"extra": {"lane_id": lid}})
                        l_ctx = self.reader.fetch_lane_context(lid)
                        l_art = self.builder.build_lane_texts(ctx=l_ctx)
                        self.save_texts_and_vectors([l_art])
                        lane_artifacts[lid] = l_art
                        summary["counts"]["lanes"] += 1
                        self.logger.info(f"{LOG_PREFIX}[LANE] done", extra={"extra": {"lane_id": lid}})
                    except Exception as e:
                        self._log_error(summary, f"{LOG_PREFIX}[LANE] lane({lid}) failed: {e}")
            except Exception as e:
                self._log_error(summary, f"{LOG_PREFIX}[LANE] batch in process({proc_id}) failed: {e}")

            # 3) Process
            try:
                self.logger.info(f"{LOG_PREFIX}[PROCESS] build start", extra={"extra": {"process_id": proc_id}})
                p_art = self.builder.build_process_texts(ctx=p_ctx, larts=lane_artifacts)
                self.save_texts_and_vectors([p_art])
                process_artifacts[proc_id] = p_art
                summary["counts"]["processes"] += 1
                self.logger.info(f"{LOG_PREFIX}[PROCESS] build done", extra={"extra": {"process_id": proc_id}})
            except Exception as e:
                self._log_error(summary, f"{LOG_PREFIX}[PROCESS] build_process_texts({proc_id}) failed: {e}")

        # ─────────────────────────────────────────────────────────────
        # 4) Participants — assemble lower-level process texts
        # ─────────────────────────────────────────────────────────────
        for p in participants:
            pid = p.get("id")
            if pid is None:
                continue
            try:
                proc_ids = (proc_ids_by_part.get(pid, []) if 'proc_ids_by_part' in locals() else [])
                arts = [process_artifacts[i] for i in proc_ids if i in process_artifacts]

                self.logger.info(
                    f"{LOG_PREFIX}[PARTICIPANT] build start",
                    extra={"extra": {"participant_id": pid, "attached_processes": len(arts)}},
                )

                part_art = self.builder.build_participant_texts(
                    ctx={"participant": p, "processes": [{"id": i, "name": (proc_name_index.get(i) if 'proc_name_index' in locals() else None)} for i in proc_ids]},
                    process_artifacts=arts,
                )

                self.save_texts_and_vectors([part_art])
                participant_artifacts.append(part_art)
                summary["counts"]["participants"] += 1

                self.logger.info(
                    f"{LOG_PREFIX}[PARTICIPANT] build done",
                    extra={"extra": {"participant_id": pid}},
                )
            except Exception as e:
                self._log_error(summary, f"{LOG_PREFIX}[PARTICIPANT] participant(pid={pid}) failed: {e}")

        # ─────────────────────────────────────────────────────────────
        # 5) Model — assemble participant texts
        # ─────────────────────────────────────────────────────────────
        try:
            self.logger.info(f"{LOG_PREFIX}[MODEL] build start", extra={"extra": {"participants": len(participants)}})
            model_art = self.builder.build_model_texts(
                ctx={"model": model_meta, "participants": participants},
                participant_artifacts=participant_artifacts,
            )
            self.save_texts_and_vectors([model_art])
            summary["counts"]["model"] = 1
            self.logger.info(f"{LOG_PREFIX}[MODEL] build done")
        except Exception as e:
            self._log_error(summary, f"{LOG_PREFIX}[MODEL] build_model_texts failed: {e}")

        self.logger.info(f"{LOG_PREFIX}[SUMMARY] done", extra={"extra": summary["counts"]})
        return summary

    # ----------------
    # Persistence API
    # ----------------
    def save_texts_and_vectors(self, artifacts: List[Dict[str, Any]]) -> int:
        """
        Persist a list of artifacts to Neo4j.
        Each artifact must contain:
          node_id, full_prop, emb_prop, full_text, (summary_prop/summary_text optional), vector (optional).
        If vector is missing, compute on the fly via builder.embedder.
        If summary_prop is missing, it will not be written.
        Non-functional: structured logs + try/except.
        """
        self.logger.info(f"{LOG_PREFIX}[SAVE] start", extra={"extra": {"count": len(artifacts or [])}})
        ok = 0
        for art in artifacts or []:
            try:
                node_id = art["node_id"]

                full_prop  = art.get("full_prop")
                sum_prop   = art.get("summary_prop")
                emb_prop   = art.get("emb_prop")
                full_txt   = art.get("full_text")
                sum_txt    = art.get("summary_text")
                vec        = art.get("vector")

                # Compute vector if missing and full text exists
                try:
                    if vec is None and full_txt is not None:
                        self.logger.debug(f"{LOG_PREFIX}[SAVE] embedding start", extra={"extra": {"id": node_id}})
                        vec = self.embedder.embed(full_txt)
                        art["vector"] = vec
                        self.logger.debug(f"{LOG_PREFIX}[SAVE] embedding done", extra={"extra": {"id": node_id}})
                except Exception as e_emb:
                    self.logger.exception(f"{LOG_PREFIX}[SAVE] embedding failed: id={node_id} err={e_emb}")
                    # continue; we might still persist texts without vector

                # Build properties to update
                props = {}
                if full_prop and full_txt is not None:
                    props[full_prop] = full_txt
                if sum_prop and (sum_txt is not None):
                    props[sum_prop] = sum_txt
                if emb_prop and vec is not None:
                    props[emb_prop] = vec

                if not props:
                    self.logger.warning(f"{LOG_PREFIX}[SAVE] nothing to persist", extra={"extra": {"id": node_id}})
                    continue

                # Persist via Reader
                try:
                    updated = self.reader.update_node_properties(node_id, props)
                except Exception as e_save:
                    self.logger.exception(f"{LOG_PREFIX}[SAVE] update failed: id={node_id} err={e_save}")
                    continue

                self.logger.info(
                    f"{LOG_PREFIX}[SAVE] persisted",
                    extra={"extra": {"id": node_id, "updated": updated, "keys": list(props.keys())}},
                )
                ok += 1

            except Exception as e:
                # Catch-all per artifact to continue the batch
                nid = art.get("node_id") if isinstance(art, dict) else None
                self.logger.exception(f"{LOG_PREFIX}[SAVE] artifact failed: id={nid} err={e}")
        self.logger.info(f"{LOG_PREFIX}[SAVE] done", extra={"extra": {"ok": ok}})
        return ok

    def _log_error(self, summary: Dict[str, Any], msg: str) -> None:
        try:
            summary["errors"].append(msg)
        finally:
            try:
                self.logger.error(msg)
            except Exception:
                # Swallow logging failures
                pass
