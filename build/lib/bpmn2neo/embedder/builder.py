# builder.py
import collections
import re
from typing import Dict, Any, List, Optional, Set, Tuple

from bpmn2neo.config.logger import Logger
from bpmn2neo.config.neo4j_repo import Neo4jRepository
from bpmn2neo.embedder.context_writer import ContextWriter

LOG_PREFIX = "[TEXT BUILD]"  # common logging prefix for this builder


class Builder:
    """
    Text assembly & embedding builder for BPMN.

    This class builds embedding-ready texts (full & summary) for each BPMN hierarchy level
    (Collaboration/Participant/Process/Lane/FlowNode) from *pre-fetched* contexts.

    Non-functional changes only:
      - English comments/logs
      - Block-level try/except with structured logging
      - Dependency injection validation and safe logger injection
    """

    def __init__(self, neo4j_config, openai_config, embedder, logger: Optional[Any] = None):
        """
        Initialize Neo4j repository/driver, context writer, and embedder.

        Args:
            neo4j_config: configuration for Neo4jRepository
            openai_config: configuration for ContextWriter
            embedder: exposes .embed(text) -> vector (list[float])
            logger: optional external logger injection
        """
        try:
            # Logger DI (prefer external; fallback to class logger)
            self.logger = logger if logger is not None else Logger.get_logger(self.__class__.__name__)
            self.logger.info(f"{LOG_PREFIX}[INIT] start")

            # Repository
            try:
                self.repository = Neo4jRepository(neo4j_config)
                self.logger.info(f"{LOG_PREFIX}[INIT] Neo4jRepository ready")
            except Exception as e_repo:
                self.logger.exception(f"{LOG_PREFIX}[INIT] Neo4jRepository init failed: {e_repo}")
                raise

            # ContextWriter (try logger injection if supported; fallback otherwise)
            try:
                try:
                    self.context_writer = ContextWriter(openai_config, logger=self.logger)  # preferred DI
                    self.logger.info(f"{LOG_PREFIX}[INIT] ContextWriter ready (with logger DI)")
                except TypeError:
                    self.context_writer = ContextWriter(openai_config)  # fallback if signature doesn't accept logger
                    self.logger.warning(f"{LOG_PREFIX}[INIT] ContextWriter does not accept logger injection; used fallback.")
            except Exception as e_cw:
                self.logger.exception(f"{LOG_PREFIX}[INIT] ContextWriter init failed: {e_cw}")
                raise

            # Embedder (external dependency DI)
            try:
                self.embedder = embedder
                if not hasattr(self.embedder, "embed") or not callable(getattr(self.embedder, "embed", None)):
                    raise TypeError("Embedder must expose an 'embed(text)->vector' method.")
                self.logger.info(f"{LOG_PREFIX}[INIT] Embedder ready")
            except Exception as e_emb:
                self.logger.exception(f"{LOG_PREFIX}[INIT] Embedder DI failed: {e_emb}")
                raise

            self.PROP_MAP = ("full_context", "summary_context", "context_vector")

            self.logger.info(f"{LOG_PREFIX}[INIT] done")
        except Exception:
            Logger.get_logger(self.__class__.__name__).exception(f"{LOG_PREFIX}[INIT] FAILED")
            raise

    # =========================
    # Public build entrypoints
    # =========================
    def build_model_texts(
        self,
        ctx: dict,
        participant_artifacts: list[dict] | None = None,
    ) -> dict:
        """
        Model-level aggregation with optional LLM summarization (fallback to simple assembly).
        Logging policy:
          - INFO: start/end, counts, output sizes
          - DEBUG: truncated bodies
          - WARN: missing artifacts or empty LLM outputs
          - ERROR: exceptions with stack traces
        """
        import json  # keep original import locality

        # --- 0) Read model & participant indices (guarded) ---
        try:
            model = ctx.get("model") or {}
            mid   = model.get("id")
            mname = (model.get("name") or f"Collaboration {mid}").strip()

            part_name_index: dict = {}
            if isinstance(ctx.get("participant_index"), dict):
                for k, v in ctx["participant_index"].items():
                    pid = str(k).strip() if k is not None else ""
                    nm = (v.get("name") if isinstance(v, dict) else v) or ""
                    nm = nm.strip() if isinstance(nm, str) else ""
                    if pid and nm:
                        part_name_index[pid] = nm

            for p in (ctx.get("participants") or []):
                if not isinstance(p, dict):
                    continue
                iid = p.get("id")
                nm  = p.get("name")
                pid = str(iid).strip() if iid is not None else ""
                if pid and nm:
                    part_name_index[pid] = (nm or "").strip()

            arts = participant_artifacts or []
            arts_by_pid = {
                (str(a.get("node_id")).strip() if a.get("node_id") is not None else ""): a
                for a in arts if a.get("node_id") is not None
            }
            def _pname(pid: str) -> str:
                return part_name_index.get(pid) or f"Participant {pid or '?'}"

            sorted_pids = sorted(arts_by_pid.keys(), key=lambda pid: ((_pname(pid)).lower(), pid))

            self.logger.info(f"{LOG_PREFIX}[MODEL] start", extra={"extra": {"model_id": mid, "name": mname, "participants": len(sorted_pids)}})
            self.logger.debug(f"{LOG_PREFIX}[MODEL] sorted_pids={sorted_pids}")
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[MODEL] init/index error: {e}", exc_info=True)
            full_text = f"# {ctx.get('model', {}).get('name', 'Model')} Collaboration.\n\n(No participant texts due to error)"
            summary_text = f"# {ctx.get('model', {}).get('name', 'Model')} Collaboration.\n\n(No participant summaries due to error)"
            return self._make_artifact(node_id=ctx.get('model', {}).get('id'), node_name=ctx.get('model', {}).get('name'), level="model", full=full_text, summary=summary_text)

        # --- H1 headers (common to both paths) ---
        full_h1  = f"# {mname} Collaboration."
        summ_h1  = f"# {mname} Collaboration."

        # --- 1) LLM delegated path ---
        full_text, summary_text = "", ""
        try:
            if len(sorted_pids) >= 1 and getattr(self, "context_writer", None) is not None:
                part_payload = []
                for pid in sorted_pids:
                    art = arts_by_pid[pid]
                    part_payload.append({
                        "id": pid,
                        "name": _pname(pid),
                        "full_text": (art.get("full_text") or "").strip(),
                        "summary_text": (art.get("summary_text") or "").strip(),
                    })

                llm_full, llm_summary = self.context_writer.generate_model_context(
                    model={"id": mid, "name": mname},
                    participants=part_payload,
                )

                llm_full = (llm_full or "").strip()
                llm_summary = (llm_summary or "").strip()

                if llm_full:
                    full_text = f"{full_h1}\n\n{llm_full}".strip()
                if llm_summary:
                    summary_text = f"{summ_h1}\n\n{llm_summary}".strip()

                self.logger.info(f"{LOG_PREFIX}[MODEL] LLM sizes", extra={"extra": {"full": len(full_text or ""), "summary": len(summary_text or "")}})
                self.logger.debug(f"{LOG_PREFIX}[MODEL] LLM FULL (trunc): {(full_text if len(full_text) <= 2000 else (full_text[:2000] + '...'))}")
                self.logger.debug(f"{LOG_PREFIX}[MODEL] LLM SUMMARY: {summary_text}")

                if not (full_text and summary_text):
                    self.logger.warning(f"{LOG_PREFIX}[MODEL] LLM outputs empty; fallback will be used.")
            else:
                self.logger.info(f"{LOG_PREFIX}[MODEL] LLM skipped", extra={"extra": {"participants": len(sorted_pids), "context_writer": type(getattr(self, 'context_writer', None)).__name__}})
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[MODEL] LLM path error: {e}", exc_info=True)
            full_text, summary_text = "", ""

        # --- 2) Fallback simple assembly ---
        try:
            if not (full_text and summary_text):
                full_lines = [full_h1]
                summ_lines = [summ_h1]

                for pid in sorted_pids:
                    pname = _pname(pid)
                    art   = arts_by_pid[pid]
                    pf    = (art.get("full_text") or "").strip()
                    ps    = (art.get("summary_text") or "").strip()

                    full_lines.append(f"## {pname}.")
                    if pf:
                        full_lines.append(pf)

                    summ_lines.append(f"## {pname}.")
                    if ps:
                        summ_lines.append(ps)

                full_text    = "\n\n".join(full_lines).strip()
                summary_text = "\n\n".join(summ_lines).strip()

                self.logger.info(f"{LOG_PREFIX}[MODEL] fallback sizes", extra={"extra": {
                    "full": len(full_text or ""), "summary": len(summary_text or ""),
                    "h2_sections": sum(1 for ln in full_text.splitlines() if ln.startswith('## '))
                }})
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[MODEL] fallback error: {e}", exc_info=True)
            full_text = full_text or f"{full_h1}\n\n(No participant texts due to error)"
            summary_text = summary_text or f"{summ_h1}\n\n(No participant summaries due to error)"

        # --- 3) Artifact ---
        try:
            self.logger.info(f"{LOG_PREFIX}[MODEL] done", extra={"extra": {"model_id": mid, "full_chars": len(full_text or ""), "summary_chars": len(summary_text or "")}})
            return self._make_artifact(node_id=mid, node_name=mname, level="model", full=full_text, summary=summary_text)
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[MODEL] artifact build error: {e}", exc_info=True)
            return self._make_artifact(node_id=mid, node_name=mname, level="model", full=(full_text or ""), summary=(summary_text or ""))

    def build_participant_texts(self, ctx: dict, process_artifacts: list[dict] | None = None) -> dict:
        """
        Participant-level aggregation with LLM (fallback to simple concatenation).
        """
        try:
            # --- 0) Entry & indices ---
            part = ctx.get("participant") or {}
            pid  = part.get("id")
            pname = (part.get("name") or f"Participant {pid}" or "No participant").strip()

            proc_name_index: dict = {}
            pidx = ctx.get("process_index")
            if isinstance(pidx, dict):
                for k, v in pidx.items():
                    proc_id = str(k).strip() if k is not None else ""
                    nm = (v.get("name") if isinstance(v, dict) else v) or ""
                    nm = nm.strip() if isinstance(nm, str) else ""
                    if proc_id and nm:
                        proc_name_index[proc_id] = nm

            for p in (ctx.get("processes") or []):
                if not isinstance(p, dict):
                    continue
                iid = p.get("id")
                nm  = p.get("name")
                proc_id = str(iid).strip() if iid is not None else ""
                if proc_id and nm:
                    proc_name_index[proc_id] = (nm or "").strip()

            arts = process_artifacts or []
            self.logger.info(f"{LOG_PREFIX}[PARTICIPANT] start", extra={"extra": {"participant_id": pid, "name": pname, "processes": len(arts)}})

            def _proc_name(art: dict) -> str:
                proc_id = str(art.get("node_id") or "").strip()
                return proc_name_index.get(proc_id) or f"Process {proc_id or '?'}"

            arts_sorted = sorted(arts, key=lambda a: (_proc_name(a).lower(), str(a.get("node_id") or "")))
            self.logger.debug(f"{LOG_PREFIX}[PARTICIPANT] sorted_proc_ids={[a.get('node_id') for a in arts_sorted]}")
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[PARTICIPANT] init/index error: {e}", exc_info=True)
            full_text = f"### {ctx.get('participant', {}).get('name', 'Participant')}\n(No process texts due to error)"
            summary_text = f"### {ctx.get('participant', {}).get('name', 'Participant')}\n(No process summaries due to error)"
            return self._make_artifact(node_id=pid, node_name=pname, level="participant", full=full_text, summary=summary_text)

        # --- 1) LLM path ---
        full_text, summary_text = "", ""
        try:
            if len(arts_sorted) >= 1 and getattr(self, "context_writer", None) is not None:
                proc_payload = [{
                    "id": str(a.get("node_id") or "").strip(),
                    "name": _proc_name(a),
                    "full_text": (a.get("full_text") or "").strip(),
                    "summary_text": (a.get("summary_text") or "").strip(),
                } for a in arts_sorted]

                full_text, summary_text = self.context_writer.generate_participant_context(
                    participant={"id": pid, "name": pname},
                    processes=proc_payload,
                )

                self.logger.info(f"{LOG_PREFIX}[PARTICIPANT] LLM sizes", extra={"extra": {"full": len(full_text or ""), "summary": len(summary_text or "")}})
                self.logger.debug(f"{LOG_PREFIX}[PARTICIPANT] LLM FULL (trunc): {(full_text if len(full_text) <= 2000 else (full_text[:2000] + '...'))}")
                self.logger.debug(f"{LOG_PREFIX}[PARTICIPANT] LLM SUMMARY: {summary_text}")

                if not (full_text and summary_text):
                    self.logger.warning(f"{LOG_PREFIX}[PARTICIPANT] LLM outputs empty; fallback will be used.")
            else:
                self.logger.info(f"{LOG_PREFIX}[PARTICIPANT] LLM skipped", extra={"extra": {"processes": len(arts_sorted), "context_writer": type(getattr(self, 'context_writer', None)).__name__}})
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[PARTICIPANT] LLM path error: {e}", exc_info=True)
            full_text, summary_text = "", ""

        # --- 2) Fallback assembly ---
        try:
            if not (full_text and summary_text):
                full_lines, summ_lines = [], []
                for art in arts_sorted:
                    pn = _proc_name(art)
                    f = (art.get("full_text") or "").strip()
                    s = (art.get("summary_text") or "").strip()

                    if f:
                        full_lines.append(f"### {pn}\n{f}")
                    elif s:
                        full_lines.append(f"### {pn}\n{s}")

                    if s:
                        summ_lines.append(f"### {pn}\n{s}")
                    elif f:
                        summ_lines.append(f"### {pn}\n{f}")

                full_text = "\n\n".join(full_lines).strip() if full_lines else f"### {pname}\n(No process texts)"
                summary_text = "\n\n".join(summ_lines).strip() if summ_lines else f"### {pname}\n(No process summaries)"

                self.logger.info(f"{LOG_PREFIX}[PARTICIPANT] fallback sizes", extra={"extra": {"full": len(full_text or ""), "summary": len(summary_text or "")}})
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[PARTICIPANT] fallback error: {e}", exc_info=True)
            full_text = full_text or f"### {pname}\n(No process texts due to error)"
            summary_text = summary_text or f"### {pname}\n(No process summaries due to error)"

        # --- 3) Artifact ---
        try:
            sections = sum(1 for ln in full_text.splitlines() if ln.startswith("### "))
            self.logger.info(f"{LOG_PREFIX}[PARTICIPANT] done", extra={"extra": {"participant_id": pid, "full_chars": len(full_text or ""), "summary_chars": len(summary_text or ""), "h3_sections": sections}})
            return self._make_artifact(node_id=pid, node_name=pname, level="participant", full=full_text, summary=summary_text)
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[PARTICIPANT] artifact build error: {e}", exc_info=True)
            return self._make_artifact(node_id=pid, node_name=pname, level="participant", full=(full_text or ""), summary=(summary_text or ""))

    def build_process_texts(self, ctx: dict, larts: Dict[str, Dict[str, Any]] | None = None) -> dict:
        """
        Process build entry: extract signals → LLM summarization (fallback on failure) → return artifact.
        """
        # --- 0) Extract signals ---
        try:
            sig = self._extract_signals_process(ctx)
            self.logger.info(f"{LOG_PREFIX}[PROCESS] signal", extra={"extra": sig if isinstance(sig, dict) else {"raw": str(sig)}})
            pid = sig["id"] if (isinstance(sig, dict) and "id" in sig) else ''
            pname = sig["name"] if (isinstance(sig, dict) and "name" in sig) else ''
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[PROCESS] signal extraction error: {e}", exc_info=True)
            proc_raw = ctx.get("process")
            proc = proc_raw if isinstance(proc_raw, dict) else (proc_raw[0] if isinstance(proc_raw, list) and proc_raw else {})
            if not isinstance(proc, dict):
                self.logger.warning(f"{LOG_PREFIX}[PROCESS][SIG] ctx['process'] unexpected type={type(proc_raw).__name__}; defaulting to {{}}")
                proc = {}
            pid = proc.get("id")
            pname = proc.get("name") or f"Process {pid}"
            sig = {"id": pid or "unknown", "name": pname or "unknown"}

        # --- 1) Generate (LLM) ---
        full, summary = "", ""
        try:
            out = self.context_writer.generate_process_context(sig, larts)
            if isinstance(out, tuple) and len(out) == 2:
                full, summary = out
            else:
                raise ValueError("generate_process_context returned a non-(full, summary) shape")

            self.logger.info(f"{LOG_PREFIX}[PROCESS] gen ok", extra={"extra": {"full": len(full or ""), "summary": len(summary or "")}})
            self.logger.debug(f"{LOG_PREFIX}[PROCESS] full_text: {(full if isinstance(full, str) and len(full) <= 2000 else (str(full)[:2000] + '...'))}")
            self.logger.debug(f"{LOG_PREFIX}[PROCESS] summary_text: {summary}")

            if not (full and summary):
                self.logger.warning(f"{LOG_PREFIX}[PROCESS] empty outputs from generator")
                return
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[PROCESS] generate_process_context error: {e}", exc_info=True)
            return

        # --- 2) Artifact ---
        try:
            art = self._make_artifact(node_id=pid, node_name=pname, level="process", full=(full or ""), summary=(summary or ""))
            self.logger.info(f"{LOG_PREFIX}[PROCESS] done", extra={"extra": {"node_id": pid, "full": len(full or ""), "summary": len(summary or "")}})
            return art
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[PROCESS] artifact build error: {e}", exc_info=True)
            return self._make_artifact(node_id=pid, node_name=pname, level="process", full=(full or ""), summary=(summary or ""))

    def build_lane_texts(self, ctx: dict) -> dict:
        """
        Lane build entry: extract lane signals → LLM summarization (fallback on failure) → return artifact.
        """
        try:
            sig = self._extract_signals_lane(ctx)
            self.logger.info(f"{LOG_PREFIX}[LANE] signal", extra={"extra": sig if isinstance(sig, dict) else {"raw": str(sig)}})
            lid = sig["id"] if (isinstance(sig, dict) and "id" in sig) else ''
            lname = sig["name"] if (isinstance(sig, dict) and "name" in sig) else ''
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[LANE] signal extraction error: {e}", exc_info=True)
            lane_raw = ctx.get("lane")
            lane = lane_raw if isinstance(lane_raw, dict) else (lane_raw[0] if isinstance(lane_raw, list) and lane_raw else {})
            if not isinstance(lane, dict):
                self.logger.warning(f"{LOG_PREFIX}[LANE][SIG] ctx['lane'] unexpected type={type(lane_raw).__name__}; defaulting to {{}}")
                lane = {}
            lid = lane.get("id")
            lname = lane.get("name") or f"Process {lid}"
            sig = {"id": lid or "unknown", "name": lname or "unknown"}

        try:
            full, summary = self.context_writer.generate_lane_context(sig)
            if not (full and summary):
                self.logger.warning(f"{LOG_PREFIX}[LANE] empty outputs from generator")
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[LANE] generate_lane_context error: {e}", exc_info=True)
            full, summary = "", ""

        return self._make_artifact(node_id=lid, node_name=lname, level="lane", full=full, summary=summary)

    def build_flownode_texts(
        self,
        node_ctx: Dict[str, Any],
        process_ctx:Dict[str, Any],
        *,
        compute_vector: bool = True,
        persist: bool = False,
    ) -> Dict[str, Any]:
        """
        Build texts for a FlowNode (Activity | Event | Gateway).
        """
        try:
            node_core = node_ctx.get("node") or {}
            node_id = node_core.get("id")
            self.logger.info(f"{LOG_PREFIX}[NODE] start", extra={"extra": {
                "node_id": node_id, "name": node_core.get("name"), "kind": node_core.get("kind"),
                "compute_vector": compute_vector, "persist": persist
            }})
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[NODE] start log failed: {e}", exc_info=True)
            node_core = node_ctx.get("node") or {}
            node_id = node_core.get("id")

        # Extract signals
        try:
            combined_ctx = dict(node_ctx)
            sig = self._extract_signals_flownode(ctx=combined_ctx, pctx=process_ctx)
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[NODE] _extract_signals_flownode error: {e}", exc_info=True)
            sig = {"node": {"id": node_id, "name": node_core.get("name"), "kind": node_core.get("kind")}}

        # Render full text
        try:
            full_text = self._render_flownode(sig)
            if not full_text:
                nm = (sig.get("node") or {}).get("name", "This node")
                full_text = f"{nm} operates within its process context and connects to adjacent steps."
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[NODE] _render_flownode error: {e}", exc_info=True)
            full_text = (node_core.get("name") or "This node") + " operates within its process context and connects to adjacent steps."

        # Compute vector (optional)
        try:
            vector = self.embedder.embed(full_text) if compute_vector else None
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[NODE] embedding error: {e}", exc_info=True)
            vector = None

        # Names and metrics
        try:
            full_prop, summary_prop, emb_prop = self.PROP_MAP
            preds = len(sig.get("predecessors", []) or [])
            succs = len(sig.get("successors", []) or [])
            msg_in = len(sig.get("messages", {}).get("in") or [])
            msg_out = len(sig.get("messages", {}).get("out") or [])
            self.logger.info(f"{LOG_PREFIX}[NODE] texts", extra={"extra": {
                "full_len": len(full_text or ""), "vector_dim": (len(vector) if isinstance(vector, list) else 0),
                "pred": preds, "succ": succs, "msg_in": msg_in, "msg_out": msg_out
            }})
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[NODE] metrics log failed: {e}", exc_info=True)
            full_prop, summary_prop, emb_prop = self.PROP_MAP

        # Artifact
        art = {
            "node_id": node_id,
            "full_prop": full_prop,
            "emb_prop": emb_prop,
            "full_text": full_text,
            "vector": vector,
        }

        # Optional persistence
        if persist and node_id is not None:
            try:
                props = {full_prop: full_text}
                if vector is not None:
                    props[emb_prop] = vector
                self._persist_props(node_id, props)
            except Exception as e:
                self.logger.error(f"{LOG_PREFIX}[NODE] persistence error: {e}", exc_info=True)

        try:
            self.logger.info(f"{LOG_PREFIX}[NODE] done", extra={"extra": {"node_id": node_id}})
        except Exception:
            pass
        return art

    # ---------------------------------------------------------
    # Extract Node / Lane / Process Signals
    # ---------------------------------------------------------
    def _extract_signals_flownode(self, ctx: Dict[str, Any], pctx: Dict[str, Any]) -> Dict[str, Any]:
        """
        FlowNode signal extraction (minimal change).
        Adds:
          1) Message details for OUT and IN directions.
          2) Data I/O names formatted as "name (state)" when state exists.
        """
        lg = self.logger
        sig: Dict[str, Any] = {}

        try:
            # --- Core node info ---
            n = ctx.get("node") or {}
            nid = n.get("id")
            sig["node"] = {
                "id": nid,
                "name": n.get("name"),
                "kind": n.get("kind"),
                "activityType": n.get("activityType"),
                "position": n.get("position"),
                "detailType": n.get("detailType"),
                "gatewayDirection": n.get("gatewayDirection"),
                "gatewayDefault": n.get("gatewayDefault"),
                "ownerLaneId": n.get("ownerLaneId"),
                "ownerProcessId": n.get("ownerProcessId"),
                "timeDate": n.get("timeDate"),
                "timeDuration": n.get("timeDuration"),
                "timeCycle": n.get("timeCycle"),
                "condition": n.get("condition"),
                "waitForCompletion": n.get("waitForCompletion"),
            }

            # --- Process context for neighbor/lane names ---
            pctx = pctx or {}
            nodes_all = (pctx.get("nodes", {}) or {}).get("all", []) or []
            nodes_by_id = {x["id"]: x for x in nodes_all}
            lanes = pctx.get("lanes", []) or []
            lane_name = {ln["id"]: ln["name"] for ln in lanes if ln and "id" in ln}

            sig["nodes_by_id"] = nodes_by_id
            sig["lane_name"] = lane_name

            # --- Sequence flows: predecessors / successors ---
            in_flows, out_flows = [], []
            for sf in ctx.get("seq_flows", []) or []:
                if sf.get("tgt") == nid:
                    in_flows.append(sf)
                if sf.get("src") == nid:
                    out_flows.append(sf)

            def _mk_neighbor(sf: Dict[str, Any], direction: str) -> Dict[str, Any]:
                other_id = sf["src"] if direction == "in" else sf["tgt"]
                other = nodes_by_id.get(other_id, {})
                return {
                    "id": other_id,
                    "name": other.get("name") or f"Node {other_id}",
                    "kind": other.get("kind"),
                    "laneId": other.get("ownerLaneId"),
                    "condition": sf.get("condition") or "",
                    "isDefault": bool(sf.get("isDefault")),
                    "flowName": sf.get("flowName") or "",
                }

            predecessors = [_mk_neighbor(sf, "in") for sf in in_flows]
            successors   = [_mk_neighbor(sf, "out") for sf in out_flows]
            sig["predecessors"] = predecessors
            sig["successors"]   = successors

            # --- Lane handoffs flags ---
            my_lane = n.get("ownerLaneId")
            to_lanes = {x["laneId"] for x in successors if x.get("laneId") is not None and x.get("laneId") != my_lane}
            from_lanes = {x["laneId"] for x in predecessors if x.get("laneId") is not None and x.get("laneId") != my_lane}
            sig["handoffs"] = {
                "to": [lane_name.get(x, f"Lane {x}") for x in sorted(list(to_lanes))],
                "from": [lane_name.get(x, f"Lane {x}") for x in sorted(list(from_lanes))],
                "present": bool(to_lanes or from_lanes),
            }

            # --- Data I/O: combine name + state ---
            def _combine_name_state(name: Optional[str], state: Optional[str]) -> str:
                nm = (name or "").strip()
                st = (state or "").strip()
                return f"{nm} ({st})" if nm and st else nm

            stores_read, stores_write, objs_read, objs_write = [], [], [], []
            io = ctx.get("data_io", {}) or {}

            for r in io.get("reads", []) or []:
                nm = _combine_name_state(
                    r.get("dataName"),
                    r.get("dataRefState") or r.get("state") or r.get("dataState")
                )
                if not nm:
                    continue
                if self._is_store(r):
                    self._push_if(stores_read, nm)
                else:
                    self._push_if(objs_read, nm)

            for w in io.get("writes", []) or []:
                nm = _combine_name_state(
                    w.get("dataName"),
                    w.get("dataRefState") or w.get("state") or w.get("dataState")
                )
                if not nm:
                    continue
                if self._is_store(w):
                    self._push_if(stores_write, nm)
                else:
                    self._push_if(objs_write, nm)

            sig["data"] = {
                "stores_read": self._uniq(stores_read),
                "stores_write": self._uniq(stores_write),
                "objects_read": self._uniq(objs_read),
                "objects_write": self._uniq(objs_write),
            }

            # --- Messages: detailed lists + topics sets ---
            topics_out, topics_in = set(), set()
            messages_out_detail, messages_in_detail = [], []

            for mf in ctx.get("msg_flows", []) or []:
                topic = (mf.get("messageRef") or "").strip() or (mf.get("flowName") or "").strip()
                if not topic:
                    continue

                src = mf.get("src")
                tgt = mf.get("tgt")

                tgt_part = mf.get("targetParticipantName") or mf.get("to_participant")
                src_part = mf.get("sourceParticipantName") or mf.get("from_participant")

                tgt_name = mf.get("targetName")
                src_name = mf.get("sourceName")

                if src == nid:
                    topics_out.add(topic)
                    messages_out_detail.append({
                        "topic": topic,
                        "to_participant": tgt_part or "Unknown",
                        "to_node": tgt_name,
                        "targetId": tgt,
                    })
                elif tgt == nid:
                    topics_in.add(topic)
                    messages_in_detail.append({
                        "topic": topic,
                        "from_participant": src_part or "Unknown",
                        "from_node": src_name,
                        "sourceId": src,
                    })

            sig["messages"] = {"out": topics_out, "in": topics_in}
            sig["messages_out"] = messages_out_detail
            sig["messages_in"]  = messages_in_detail

            # --- Annotations ---
            quotes = []
            for a in ctx.get("annotations", []) or []:
                txt = (a.get("text") or "").strip()
                if txt:
                    quotes.append(self._truncate(txt, 120))
            sig["annotations"] = self._uniq(quotes)[:3]

            # --- Boundary events (optional) ---
            b_list = []
            for be in ctx.get("boundary_events", []) or []:
                dt = be.get("detailType")
                intr = be.get("isInterrupting")
                if dt:
                    b_list.append(f"{dt}{' (non-interrupting)' if intr is False else ''}")
            sig["boundary"] = {"attached": self._uniq(b_list)}
        except Exception as e:
            lg.exception(f"{LOG_PREFIX}[NODE][SIG] FAILED: {e}")
        return sig

    def _extract_signals_lane(self, ctx: dict) -> dict:
        """
        Lane-level signal extraction aligned to _extract_signals_process.
        """
        lg = self.logger

        def _type_name(x):
            try:
                return type(x).__name__
            except Exception:
                return "<unknown>"

        def _head_list(xs, n=3):
            try:
                return [xs[i] for i in range(min(len(xs), n))]
            except Exception:
                return "<unrepr>"

        try:
            lg.info(f"{LOG_PREFIX}[LANE][SIG] start; ctx_keys={list(ctx.keys())}")
        except Exception:
            lg.info(f"{LOG_PREFIX}[LANE][SIG] start; ctx_keys=<unavailable>")

        try:
            # 1) Lane maps
            try:
                lane_name_by_id, node_lane_by_id = self._build_lane_maps(ctx.get("lanes"))
                lg.info(f"{LOG_PREFIX}[LANE][SIG] lane_maps built", extra={"extra": {"lanes": len(lane_name_by_id or {})}})
            except Exception as e_maps:
                lg.exception(f"{LOG_PREFIX}[LANE][SIG] build lane maps failed: {e_maps}")
                lane_name_by_id, node_lane_by_id = {}, {}

            # 2) Lane meta
            try:
                lane = ctx.get("lane") or {}
                lane_id = lane.get("id")
                lane_name = lane.get("name")
                lane_node_ids = set()

                lanes_arr = ctx.get("lanes") or []
                for l in lanes_arr:
                    if isinstance(l, dict) and l.get("id") == lane_id and l.get("nodeIds"):
                        for nid in (l.get("nodeIds") or []):
                            lane_node_ids.add(nid)
                        break

                if not lane_node_ids and lane.get("nodes"):
                    for x in lane["nodes"]:
                        if isinstance(x, int):
                            lane_node_ids.add(x)
                        elif isinstance(x, dict) and x.get("id") is not None:
                            lane_node_ids.add(x.get("id"))

                if not lane_node_ids:
                    lane_node_ids = {nid for nid, lid in (node_lane_by_id or {}).items() if lid == lane_id}

                lg.info(f"{LOG_PREFIX}[LANE][SIG] lane meta", extra={"extra": {"lane_id": lane_id, "name": lane_name, "node_count": len(lane_node_ids)}})
            except Exception as e_lane:
                lg.exception(f"{LOG_PREFIX}[LANE][SIG] lane meta/nodes failed: {e_lane}")
                lane_id = (ctx.get("lane") or {}).get("id")
                lane_name = (ctx.get("lane") or {}).get("name") or "Lane"
                lane_node_ids = set()

            # 3) Messages (filtered to lane nodes)
            try:
                msg_flows = ctx.get("message_flows") or ctx.get("msg_flows") or []
                messages = self._build_message_details(
                    msg_flows_raw=msg_flows,
                    scope_node_ids=lane_node_ids
                )
                lg.info(f"{LOG_PREFIX}[LANE][SIG] messages", extra={"extra": {"out": len(messages.get('out', [])), "in": len(messages.get('in', []))}})
            except Exception as e_msg:
                lg.exception(f"{LOG_PREFIX}[LANE][SIG] messages build failed: {e_msg}")
                messages = {"out": [], "in": []}

            # 4) Lane handoffs (edge-wise)
            try:
                raw = ctx.get("lane_handoffs") or []
                if not isinstance(raw, list):
                    lg.warning(f"{LOG_PREFIX}[LANE][SIG] ctx['lane_handoffs'] type={_type_name(raw)}; coerced to []")
                    raw = []

                inter_lane_handoffs = []
                for h in raw:
                    if not isinstance(h, dict):
                        continue
                    sl, tl = h.get("srcLane"), h.get("tgtLane")
                    if sl is None or tl is None or lane_id is None or sl == tl:
                        continue

                    if sl == lane_id:
                        inter_lane_handoffs.append({
                            "handoff_type": "seq_to",
                            "lane_id":   tl,
                            "lane_name": h.get("tgtLaneName") or lane_name_by_id.get(tl, tl),
                            "node_id":   h.get("tgtNode"),
                            "node_name": h.get("tgtNodeName"),
                        })
                    elif tl == lane_id:
                        inter_lane_handoffs.append({
                            "handoff_type": "seq_from",
                            "lane_id":   sl,
                            "lane_name": h.get("srcLaneName") or lane_name_by_id.get(sl, sl),
                            "node_id":   h.get("srcNode"),
                            "node_name": h.get("srcNodeName"),
                        })

                lg.info(f"{LOG_PREFIX}[LANE][SIG] lane_handoffs", extra={"extra": {"edges": len(inter_lane_handoffs), "sample": _head_list(inter_lane_handoffs, 3)}})
            except Exception as e_hx:
                lg.exception(f"{LOG_PREFIX}[LANE][SIG] lane_handoffs transform failed: {e_hx}")
                inter_lane_handoffs = []

            # 5) Data I/O (reads/writes)
            try:
                dio = ctx.get("data_io") or {}
                reads = dio.get("reads") if isinstance(dio, dict) else None
                writes = dio.get("writes") if isinstance(dio, dict) else None
                if reads is None:
                    reads = ctx.get("data_reads")
                if writes is None:
                    writes = ctx.get("data_writes")

                stores_r, objs_r = self._pack_io(reads, "data_reads")
                stores_w, objs_w = self._pack_io(writes, "data_writes")
                data_io = {
                    "reads":  {"stores": stores_r, "objects": objs_r},
                    "writes": {"stores": stores_w, "objects": objs_w},
                }
                lg.info(f"{LOG_PREFIX}[LANE][SIG] data_io", extra={"extra": {
                    "reads_stores": len(stores_r), "reads_objects": len(objs_r),
                    "writes_stores": len(stores_w), "writes_objects": len(objs_w)
                }})
            except Exception as e_io:
                lg.exception(f"{LOG_PREFIX}[LANE][SIG] data_io build failed: {e_io}")
                data_io = {"reads": {"stores": [], "objects": []}, "writes": {"stores": [], "objects": []}}

            # 6) Paths payloads (pass-through)
            try:
                paths_all = ctx.get("paths_all")
                if paths_all is None:
                    lg.warning(f"{LOG_PREFIX}[LANE][SIG] ctx['paths_all'] missing; set to {{}}")
                    paths_all = {}
                elif not isinstance(paths_all, dict):
                    lg.warning(f"{LOG_PREFIX}[LANE][SIG] ctx['paths_all'] expected dict but got {_type_name(paths_all)}; passing through as-is")
                subprocess_paths_all = []
            except Exception as e_paths:
                lg.exception(f"{LOG_PREFIX}[LANE][SIG] paths build failed: {e_paths}")
                paths_all = {}
                subprocess_paths_all = []

            # 7) Assemble output
            try:
                out = {
                    "id": lane_id,
                    "name": lane_name,
                    "paths_all": paths_all,
                    "subprocess_paths_all": subprocess_paths_all,
                    "messages": messages,
                    "lane_handoffs": inter_lane_handoffs,
                    "data_io": data_io,
                }
            except Exception as e_asm:
                lg.exception(f"{LOG_PREFIX}[LANE][SIG] assemble output failed: {e_asm}")
                out = {
                    "id": lane_id,
                    "name": lane_name or "Lane",
                    "paths_all": {},
                    "subprocess_paths_all": [],
                    "messages": {"out": [], "in": []},
                    "lane_handoffs": [],
                    "data_io": {"reads": {"stores": [], "objects": []}, "writes": {"stores": [], "objects": []}},
                }

            # 8) Final metrics log
            try:
                msgs_out_cnt = len(out["messages"].get("out", [])) if isinstance(out.get("messages"), dict) else 0
                msgs_in_cnt  = len(out["messages"].get("in", []))  if isinstance(out.get("messages"), dict) else 0
                subproc_cnt  = len(out.get("subprocess_paths_all") or [])
                handoffs_cnt = len(out.get("lane_handoffs") or [])
                data_io_cnt  = len(out.get("data_io") or {})
                lg.info(f"{LOG_PREFIX}[LANE][SIG] done", extra={"extra": {
                    "msgs_out": msgs_out_cnt, "msgs_in": msgs_in_cnt,
                    "paths_main_type": ("dict" if isinstance(out.get("paths_all"), dict) else _type_name(out.get("paths_all"))),
                    "subproc": subproc_cnt, "lane_handoffs": handoffs_cnt, "data_io_keys": data_io_cnt
                }})
            except Exception as e_done:
                lg.exception(f"{LOG_PREFIX}[LANE][SIG] finalize log failed: {e_done}")

            return out

        except Exception as e:
            lg.exception(f"{LOG_PREFIX}[LANE][SIG] FAILED (outer): {e}")
            return {
                "id": (ctx.get("lane") or {}).get("id"),
                "name": (ctx.get("lane") or {}).get("name") or "Lane",
                "paths_all": {},
                "subprocess_paths_all": [],
                "messages": {"out": [], "in": []},
                "lane_handoffs": [],
                "data_io": {"reads": {"stores": [], "objects": []}, "writes": {"stores": [], "objects": []}},
            }

    def _extract_signals_process(self, ctx: dict) -> dict:
        """
        Process-level signal extraction (lane-agnostic, simplified).
        """
        lg = self.logger

        def _type_name(x): return type(x).__name__
        def _head_list(xs, n=3):
            try:
                return [xs[i] for i in range(min(len(xs), n))]
            except Exception:
                return "<unrepr>"

        try:
            lg.info(f"{LOG_PREFIX}[PROCESS][SIG] start; ctx_keys={list(ctx.keys())}")
        except Exception:
            lg.info(f"{LOG_PREFIX}[PROCESS][SIG] start; ctx_keys=<unavailable>")

        # process meta
        proc_raw = ctx.get("process")
        proc = proc_raw if isinstance(proc_raw, dict) else (proc_raw[0] if isinstance(proc_raw, list) and proc_raw else {})
        if not isinstance(proc, dict):
            lg.warning(f"{LOG_PREFIX}[PROCESS][SIG] ctx['process'] unexpected type={_type_name(proc_raw)}; defaulting to {{}}")
            proc = {}
        pid = proc.get("id")
        pname = proc.get("name") or f"Process {pid}"
        lg.debug(f"{LOG_PREFIX}[PROCESS][SIG] process: id={pid} name={pname}")

        # nodes
        nodes_all = (ctx.get("nodes", {}) or {}).get("all", []) or []
        all_node_ids = {x["id"]: x for x in nodes_all}
        scope_node_ids = set(all_node_ids.keys())
        lg.info(f"{LOG_PREFIX}[PROCESS][SIG] nodes total={len(all_node_ids)}")

        # messages
        try:
            msg_flows = ctx.get("msg_flows") or ctx.get("message_flows") or []
            messages = self._build_message_details(
                msg_flows_raw=msg_flows,
                scope_node_ids=scope_node_ids
            )
            lg.debug(f"{LOG_PREFIX}[PROCESS][SIG] messages details: out={len(messages.get('out', []))} in={len(messages.get('in', []))}")
        except Exception as e_msg:
            lg.exception(f"{LOG_PREFIX}[PROCESS][SIG] messages build failed: {e_msg}")
            messages = {"out": [], "in": []}

        # lanes
        lanes_raw = ctx.get("lanes") or []
        if isinstance(lanes_raw, dict):
            lanes = [lanes_raw]
            lg.warning(f"{LOG_PREFIX}[PROCESS][SIG] ctx['lanes'] is dict; coerced to singleton list.")
        elif isinstance(lanes_raw, list):
            lanes = lanes_raw
        else:
            lanes = []
            lg.warning(f"{LOG_PREFIX}[PROCESS][SIG] ctx['lanes'] unexpected type={_type_name(lanes_raw)}; treated as []")

        try:
            lane_name_by_id, node_lane_by_id = self._build_lane_maps(ctx.get("lanes"))
            lane_node_ids_union = set(node_lane_by_id.keys())
            lg.info(f"{LOG_PREFIX}[PROCESS][SIG] lanes={len(lane_name_by_id)} has_lanes={bool(lane_name_by_id)} lane_nodes_union={len(lane_node_ids_union)}")
        except Exception as e_maps:
            lg.exception(f"{LOG_PREFIX}[PROCESS][SIG] build lane maps failed: {e_maps}")
            lane_name_by_id, node_lane_by_id = {}, {}

        # inter-lane handoffs
        try:
            raw_handoffs = ctx.get("lane_handoffs") or []
            if not isinstance(raw_handoffs, list):
                lg.warning(f"{LOG_PREFIX}[PROCESS][SIG] ctx['lane_handoffs'] type={_type_name(raw_handoffs)}; coerced to []")
                raw_handoffs = []

            inter_lane_handoffs = []
            for h in raw_handoffs:
                if not isinstance(h, dict):
                    continue
                src_id = h.get("src") or h.get("srcNode")
                tgt_id = h.get("tgt") or h.get("tgtNode")
                inter_lane_handoffs.append({
                    "src": src_id,
                    "tgt": tgt_id,
                    "srcNodeName": h.get("srcNodeName"),
                    "tgtNodeName": h.get("tgtNodeName"),
                    "srcLane": h.get("srcLane"),
                    "tgtLane": h.get("tgtLane"),
                    "srcLaneName": h.get("srcLaneName"),
                    "tgtLaneName": h.get("tgtLaneName"),
                    "seqId": h.get("seqId"),
                    "condition": h.get("condition"),
                    "isDefault": bool(h.get("isDefault", False)),
                })

            lg.info(f"{LOG_PREFIX}[PROCESS][SIG] inter_lane_handoffs", extra={"extra": {"edges": len(inter_lane_handoffs), "sample": _head_list(inter_lane_handoffs, 3)}})
        except Exception as e_hx:
            lg.exception(f"{LOG_PREFIX}[PROCESS][SIG] lane_handoffs transform failed: {e_hx}")
            inter_lane_handoffs = []

        # data I/O
        try:
            dio = ctx.get("data_io") or {}
            reads = dio.get("reads") if isinstance(dio, dict) else None
            writes = dio.get("writes") if isinstance(dio, dict) else None
            if reads is None:
                reads = ctx.get("data_reads")
            if writes is None:
                writes = ctx.get("data_writes")

            stores_r, objs_r = self._pack_io(reads, "data_reads")
            stores_w, objs_w = self._pack_io(writes, "data_writes")
            data_io = {
                "reads":  {"stores": stores_r, "objects": objs_r},
                "writes": {"stores": stores_w, "objects": objs_w},
            }
            lg.info(f"{LOG_PREFIX}[PROCESS][SIG] data_io", extra={"extra": {
                "reads_stores": len(stores_r), "reads_objects": len(objs_r),
                "writes_stores": len(stores_w), "writes_objects": len(objs_w)
            }})
        except Exception as e_io:
            lg.exception(f"{LOG_PREFIX}[PROCESS][SIG] data_io build failed: {e_io}")
            data_io = {"reads": {"stores": [], "objects": []}, "writes": {"stores": [], "objects": []}}

        # paths payloads
        try:
            paths_all = ctx.get("paths_all")
            if paths_all is None:
                lg.warning(f"{LOG_PREFIX}[PROCESS][SIG] ctx['paths_all'] missing; set to {{}}")
                paths_all = {}
            elif not isinstance(paths_all, dict):
                lg.warning(f"{LOG_PREFIX}[PROCESS][SIG] ctx['paths_all'] expected dict but got {_type_name(paths_all)}; passing through as-is")

            subprocess_paths_all = ctx.get("subprocess_paths_all") or []
            if not isinstance(subprocess_paths_all, list):
                lg.warning(f"{LOG_PREFIX}[PROCESS][SIG] ctx['subprocess_paths_all'] type={_type_name(subprocess_paths_all)}; coerced to []")
                subprocess_paths_all = []
        except Exception as e_paths:
            lg.exception(f"{LOG_PREFIX}[PROCESS][SIG] paths payload build failed: {e_paths}")
            paths_all = {}
            subprocess_paths_all = []

        # assemble output
        try:
            out = {
                "id": pid,
                "name": pname,
                "paths_all": paths_all,
                "subprocess_paths_all": subprocess_paths_all,
                "messages": messages,
                "inter_lane_handoffs": inter_lane_handoffs,
                "data_io": data_io
            }
        except Exception as e_asm:
            lg.exception(f"{LOG_PREFIX}[PROCESS][SIG] assemble output failed: {e_asm}")
            out = {
                "id": pid,
                "name": pname,
                "paths_all": {},
                "subprocess_paths_all": [],
                "messages": {"out": [], "in": []},
                "inter_lane_handoffs": [],
                "data_io": {"reads": {"stores": [], "objects": []}, "writes": {"stores": [], "objects": []}},
            }

        # final log
        try:
            msgs_out = len((out.get("messages") or {}).get("out", []))
            msgs_in  = len((out.get("messages") or {}).get("in", []))
            paths_main = "dict" if isinstance(out.get("paths_all"), dict) else _type_name(out.get("paths_all"))
            subproc_count   = len(out.get("subprocess_paths_all") or [])
            handoffs_count  = len(out.get("inter_lane_handoffs") or [])
            data_io_count   = len(out.get("data_io") or {})
            lg.info(f"{LOG_PREFIX}[PROCESS][SIG] done", extra={"extra": {
                "msgs_out": msgs_out, "msgs_in": msgs_in,
                "paths_main_type": paths_main, "subproc": subproc_count, "inter_lane_handoffs": handoffs_count, "data_io_keys": data_io_count
            }})
        except Exception as e_done:
            lg.exception(f"{LOG_PREFIX}[PROCESS][SIG] finalize log failed: {e_done}")

        return out

    # ---------------------------------------------------------
    # Render Node
    # ---------------------------------------------------------
    def _render_flownode(self, s: dict) -> tuple[str, str]:
        """
        Deterministic text for a single FlowNode, aligned with the updated
        _extract_signals_flownode signature.

        Notes
        - Message examples include target/source participant and node names.
        - Data I/O lists already include "name (state)" strings when state exists.
        - Summary text is not required for flownodes (returns "").
        """
        lines: list[str] = []

        node = s.get("node")

        # 1) Node nature
        nm = node.get("name") or "This node"
        kind = (node.get("kind") or "").lower()
        if "event" in kind:
            pos = (node.get("position") or "").strip()
            det = (node.get("detailType") or "").strip()
            tag_parts = []
            tag_parts.append("Event ")
            if pos:
                tag_parts.append(f"Position : {pos.capitalize()},")
            if det:
                tag_parts.append(f"detailType : {det.capitalize()}")
            tag = " ".join(tag_parts)
            self._push_if(lines, f"{tag} '{nm}'.")
        elif "gateway" in kind:
            gd = (node.get("gatewayDirection") or "").strip()
            tag = f"Gateway Direction : {gd.upper()}" if gd else "Gateway"
            self._push_if(lines, f"{tag} 'Name : {nm}'.")
        else:
            type = (node.get("activityType") or "").strip()
            tag = f"Activity Type : {type.upper()}" if type else "Activity"
            self._push_if(lines, f"{tag} 'Name : {nm}'.")

        # 2) Messages (OUT/IN)
        mo = s.get("messages_out") or []
        mi = s.get("messages_in") or []
        topics_fallback = s.get("messages") or {}
        topics_out_fb = list(topics_fallback.get("out") or [])[:2]
        topics_in_fb  = list(topics_fallback.get("in") or [])[:2]

        if mo:
            examples = []
            for m in mo[:2]:
                topic = m.get("topic")
                if not topic:
                    continue
                to_part = m.get("to_participant") or "Unknown"
                to_node = m.get("to_node") or "Unknown"
                examples.append(f"'{topic}' to Participant '{to_part}' (node '{to_node}')")
            if examples:
                self._push_if(lines, f"It sends business messages such as {', '.join(examples)}.")
        elif topics_out_fb:
            self._push_if(lines, "It sends business messages such as " + ", ".join(f"'{t}'" for t in topics_out_fb) + ".")

        if mi:
            examples = []
            for m in mi[:2]:
                topic = m.get("topic")
                if not topic:
                    continue
                from_part = m.get("from_participant") or "Unknown"
                from_node = m.get("from_node") or "Unknown"
                examples.append(f"'{topic}' from Participant '{from_part}' (node '{from_node}')")
            if examples:
                self._push_if(lines, f"It receives messages such as {', '.join(examples)}.")
        elif topics_in_fb:
            self._push_if(lines, "It receives messages such as " + ", ".join(f"'{t}'" for t in topics_in_fb) + ".")

        # 3) Data I/O
        data = s.get("data") or {}
        reads_map  = data.get("reads") or {}
        writes_map = data.get("writes") or {}

        sr = s.get("stores_read") or data.get("stores_read") or (reads_map.get("stores") or [])
        or_ = s.get("objs_read")   or data.get("objects_read") or (reads_map.get("objects") or [])
        sw = s.get("stores_write") or data.get("stores_write") or (writes_map.get("stores") or [])
        ow = s.get("objs_write")   or data.get("objects_write") or (writes_map.get("objects") or [])

        def _qjoin(xs: list[str]) -> str:
            return ", ".join(f"'{x}'" for x in (xs or []))

        dio_parts: list[str] = []
        if sr or or_:
            segs = []
            if sr:  segs.append(f"stores ({_qjoin(sr)})")
            if or_: segs.append(f"objects ({_qjoin(or_)})")
            dio_parts.append("reads " + " and ".join(segs))
        if sw or ow:
            segs = []
            if sw:  segs.append(f"stores ({_qjoin(sw)})")
            if ow:  segs.append(f"objects ({_qjoin(ow)})")
            dio_parts.append("writes " + " and ".join(segs))
        if dio_parts:
            self._push_if(lines, "It " + " and ".join(dio_parts) + ".")

        # 4) Neighbors
        prevs = s.get("predecessors") or []
        nexts = s.get("successors") or []
        nb_line = self._neighbors_line(prevs, nexts)
        if nb_line:
            self._push_if(lines, nb_line)

        full = self._finalize(lines)
        return full

    # ---------------------------------------------------------
    # Persistence helper
    # ---------------------------------------------------------
    def _persist_props(self, node_id: int, props: Dict[str, Any]) -> None:
        """
        Persist properties onto a node by internal id using SET += map.
        """
        if node_id is None or not props:
            return
        cypher = """
        MATCH (n) WHERE id(n)=$id
        SET n += $props
        RETURN id(n) AS id
        """
        try:
            self.repository.execute_single_query(cypher, {"id": node_id, "props": props})
            self.logger.info(f"{LOG_PREFIX}[PERSIST] ok", extra={"extra": {"node_id": node_id, "keys": list(props.keys())}})
        except Exception as e:
            self.logger.error(f"{LOG_PREFIX}[PERSIST] fail: node_id={node_id}, err={e}")

    def _build_lane_maps(self, lanes_raw):
        """
        Build lane_name_by_id and node_lane_by_id from a flexible 'lanes' payload.
        """
        lg = self.logger
        try:
            if lanes_raw is None:
                lanes = []
            elif isinstance(lanes_raw, dict):
                lanes = [lanes_raw]
                lg.warning(f"{LOG_PREFIX}[COMMON] ctx['lanes'] is dict; coerced to singleton list.")
            elif isinstance(lanes_raw, list):
                lanes = lanes_raw
            else:
                lg.warning(f"{LOG_PREFIX}[COMMON] ctx['lanes'] unexpected type={type(lanes_raw).__name__}; treated as []")
                lanes = []

            lane_name_by_id, node_lane_by_id = {}, {}
            for l in lanes:
                if not isinstance(l, dict):
                    lg.debug(f"{LOG_PREFIX}[COMMON] lane entry type={type(l).__name__} skipped")
                    continue
                lid = l.get("id")
                if lid is not None:
                    lane_name_by_id[lid] = l.get("name") or f"Lane {lid}"
                ids = l.get("nodeIds") or l.get("nodes") or []
                for x in ids:
                    if isinstance(x, int):
                        node_lane_by_id[x] = lid
                    elif isinstance(x, dict) and "id" in x:
                        node_lane_by_id[x["id"]] = lid
                    else:
                        lg.debug(f"{LOG_PREFIX}[COMMON] lane({lid}) nodes entry type={type(x).__name__} skipped")

            lg.info(f"{LOG_PREFIX}[COMMON] lane_maps built", extra={"extra": {"lanes": len(lane_name_by_id), "bindings": len(node_lane_by_id)}})
            return lane_name_by_id, node_lane_by_id
        except Exception as e:
            lg.exception(f"{LOG_PREFIX}[COMMON] lane_maps FAILED: {e}")
            return {}, {}

    def _build_message_details(self, msg_flows_raw, scope_node_ids):
        """
        Build detailed message IN/OUT lists for a given scope of node IDs.
        """
        lg = self.logger
        try:
            if msg_flows_raw is None:
                msg_flows = []
            elif isinstance(msg_flows_raw, list):
                msg_flows = msg_flows_raw
            else:
                lg.warning(f"{LOG_PREFIX}[COMMON] message_flows type={type(msg_flows_raw).__name__}; coerced to []")
                msg_flows = []

            out_detail, in_detail = [], []
            for m in msg_flows:
                if not isinstance(m, dict):
                    continue
                topic = (m.get("flowName") or m.get("messageRef") or "").strip()
                if not topic:
                    continue

                src = m.get("src") or m.get("sourceId")
                tgt = m.get("tgt") or m.get("targetId")

                src_part = m.get("sourceParticipantName") or m.get("from_participant") or m.get("sourceParticipant")
                tgt_part = m.get("targetParticipantName") or m.get("to_participant")   or m.get("targetParticipant")

                src_name = m.get("sourceName") or (f"Node {src}" if src is not None else "Unknown node")
                tgt_name = m.get("targetName") or (f"Node {tgt}" if tgt is not None else "Unknown node")

                if src in scope_node_ids:
                    out_detail.append({
                        "topic": topic,
                        "to_participant": tgt_part or "Unknown",
                        "to_node": tgt_name,
                        "targetId": tgt,
                    })
                if tgt in scope_node_ids:
                    in_detail.append({
                        "topic": topic,
                        "from_participant": src_part or "Unknown",
                        "from_node": src_name,
                        "sourceId": src,
                    })

            lg.info(f"{LOG_PREFIX}[COMMON] messages built", extra={"extra": {"out": len(out_detail), "in": len(in_detail)}})
            return {"out": out_detail, "in": in_detail}
        except Exception as e:
            lg.exception(f"{LOG_PREFIX}[COMMON] messages FAILED: {e}")
            return {"out": [], "in": []}

    def _build_inter_lane_handoffs(self, seq_flows_raw, node_lane_by_id, lane_name_by_id, scope_lane_id=None):
        """
        Build inter-lane handoffs list from sequence flows.
        """
        lg = self.logger
        try:
            if seq_flows_raw is None:
                seq_flows = []
            elif isinstance(seq_flows_raw, list):
                seq_flows = seq_flows_raw
            else:
                lg.warning(f"{LOG_PREFIX}[COMMON] seq_flows type={type(seq_flows_raw).__name__}; coerced to []")
                seq_flows = []

            counts = {}
            for e in seq_flows:
                if not isinstance(e, dict):
                    continue
                sid = e.get("source") or e.get("sourceId")
                tid = e.get("target") or e.get("targetId")
                sl, tl = node_lane_by_id.get(sid), node_lane_by_id.get(tid)
                if sl is None or tl is None or sl == tl:
                    continue
                if scope_lane_id is not None and (sl != scope_lane_id and tl != scope_lane_id):
                    continue
                counts[(sl, tl)] = counts.get((sl, tl), 0) + 1

            out = []
            def _lname(x):
                if x is None:
                    return "Outside"
                return lane_name_by_id.get(x, x)

            for (a, b), c in counts.items():
                out.append({
                    "from_lane": _lname(a),
                    "to_lane":   _lname(b),
                    "count":     int(c),
                })
            out.sort(key=lambda d: (str(d["from_lane"]), str(d["to_lane"]), d["count"]))
            lg.info(f"{LOG_PREFIX}[COMMON] inter_lane_handoffs built", extra={"extra": {"pairs": len(out)}})
            return out
        except Exception as e:
            lg.exception(f"{LOG_PREFIX}[COMMON] inter_lane_handoffs FAILED: {e}")
            return []

    def _pack_io(self, recs, tag: str):
        """
        Keep original contract: return two lists (stores, objects).
        """
        stores, objs = [], []

        if not isinstance(recs, list):
            if recs is not None:
                self.logger.warning(f"{LOG_PREFIX}[PROC][SIG] {tag} type={type(recs).__name__}; coerced to []")
            recs = []

        rel = "READS_FROM" if tag == "data_reads" else "WRITES_TO"

        for r in recs:
            if not isinstance(r, dict):
                continue

            label = self._fmt_dataref(
                r.get("dataName") or r.get("dataRefName") or "",
                r.get("dataRefState") or ""
            )

            entry = {
                "node_id": r.get("nodeId") or r.get("node"),
                "node_name": r.get("nodeName") or "",
                "rel": rel,
                "data_label": label,
                "data_id": r.get("dataId"),
                "data_ref_id": r.get("dataRefId"),
            }

            if self._is_store(r):
                stores.append(entry)
            else:
                objs.append(entry)

        return stores, objs

    def _neighbors_line(self, prevs, nexts) -> str:
        p = ", ".join(f"'{x['name']}'" for x in (prevs or [])[:3])
        n = ", ".join(f"'{x['name']}'" for x in (nexts or [])[:3])
        if p and n:  return f"It typically follows {p} and precedes {n}."
        if p:        return f"It typically follows {p}."
        if n:        return f"It typically precedes {n}."
        return ""

    def _fmt_dataref(self, name: str | None, state: str | None) -> str:
        """Return 'name (state)' if state exists; otherwise 'name'."""
        nm = (name or "").strip()
        st = (state or "").strip()
        return nm if not st else f"{nm} ({st})"

    def _uniq(self, xs, key=None, keep_none: bool = False):
        """
        Deduplicate while preserving order.
        """
        if xs is None:
            return []
        seen = set()
        out = []
        for x in xs:
            if x is None and not keep_none:
                continue
            k = key(x) if key else x
            if k not in seen:
                seen.add(k)
                out.append(x)
        return out

    def _push_if(self, out_list: list, value, *, cond: bool = True, allow_dup: bool = False):
        """
        Append value to list if valid and condition holds.
        """
        if not cond:
            return
        if value in (None, "", Ellipsis):
            return
        if allow_dup or value not in out_list:
            out_list.append(value)

    def _is_store(self, obj) -> bool:
        """
        Determine if a DataStore by 'dataRefKind' (preferred) or kind semantics.
        """
        if not isinstance(obj, dict):
            return False
        kind = obj.get("dataRefKind")
        return isinstance(kind, str) and kind.strip().lower() == "StoreReference"

    def _finalize(self, parts, joiner: str = " "):
        """
        Join fragments into a single string with light whitespace normalization.
        """
        if parts is None:
            return ""
        if isinstance(parts, str):
            text = parts
        else:
            items = []
            for p in parts:
                if p in (None, "", Ellipsis):
                    continue
                items.append(str(p).strip())
            text = joiner.join(x for x in items if x)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _make_artifact(self, node_id: int, node_name: str, level: str, full: str, summary: str) -> dict:
        full_prop, sum_prop, emb_prop = self.PROP_MAP
        return {
            "node_id": node_id,
            "node_name": node_name,
            "full_prop": full_prop,
            "summary_prop": sum_prop,
            "emb_prop": emb_prop,
            "full_text": self._finalize(full, joiner="\n\n"),
            "summary_text": self._finalize(summary, joiner=" "),
        }

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """Truncate text to a soft limit with ellipsis."""
        t = (text or "").strip()
        if len(t) <= limit:
            return t
        return t[: max(0, limit - 1)].rstrip() + "…"
