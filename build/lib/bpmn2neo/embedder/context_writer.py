# context_writer.py
from typing import Any, Dict, Optional
from openai import OpenAI

import re

from bpmn2neo.config.logger import Logger
from bpmn2neo.settings import OpenAISettings

PFX = "[CONTEXT WRITE]"  # common logging prefix

class ContextWriter:
    """
    LLM-backed context writer that produces FULL/SUMMARY texts
    for Lane / Process / Participant / Model levels from simplified signals.

    Non-functional refactor:
      - English comments & logs
      - Structured logging with a common prefix
      - Robust try/except at block level
      - Dependency Injection (logger, client) with safe fallbacks
    """

    def __init__(self, openai_config: OpenAISettings, logger: Optional[Any] = None, client: Optional[OpenAI] = None):
        """
        Args:
            openai_config: OpenAISettings with model/temperature/token limits/api_key, etc.
            logger: optional external logger (DI). Falls back to module logger if missing.
            client: optional pre-configured OpenAI client (DI). If omitted, a client is created.
        """
        # Logger DI
        self.logger = logger if logger is not None else Logger.get_logger(self.__class__.__name__)
        try:
            self.logger.info(f"{PFX}[INIT] start")

            # Config DI
            self.config = openai_config
            if self.config is None:
                raise TypeError("openai_config is required.")

            # Client DI (prefer injected; else construct from config)
            try:
                if client is not None:
                    self.client = client
                    self.logger.info(f"{PFX}[INIT] OpenAI client injected (DI).")
                else:
                    api_key = getattr(openai_config, "api_key", None)
                    self.client = OpenAI(api_key=api_key) if api_key else OpenAI()
                    self.logger.info(f"{PFX}[INIT] OpenAI client created (api_key={'set' if api_key else 'env/default'}).")
            except Exception as e_cli:
                self.logger.exception(f"{PFX}[INIT] OpenAI client initialization failed: {e_cli}")
                self.client = None  # allow fallback paths down the line

            self.logger.info(f"{PFX}[INIT] done")
        except Exception:
            Logger.get_logger(self.__class__.__name__).exception(f"{PFX}[INIT] FAILED")
            raise

    def generate_lane_context(self, signals: dict) -> tuple[str, str]:
        """
        Generate lane-level FULL/SUMMARY texts from simplified 'signals'.
        Enforces the exact scaffold and falls back heuristically on failure.
        """
        # Safe defaults for outermost exception path
        full_safe, summary_safe = "", ""
        payload_for_fallback = {
            "id": None,
            "name": None,
            "paths_all": None,
            "subprocess_paths_all": None,
            "messages": {},
            "lane_handoffs": [],
            "data_io": {},
        }

        try:
            import json

            # --- 0) Normalize input (guarded) --------------------------------------
            try:
                name = (signals or {}).get("name")
                lane_id = (signals or {}).get("id")
                paths_all = (signals or {}).get("paths_all")
                subprocess_paths_all = (signals or {}).get("subprocess_paths_all") or []
                messages = (signals or {}).get("messages") or {}
                lane_handoffs = (signals or {}).get("lane_handoffs") or []
                data_io = (signals or {}).get("data_io") or {}

                payload_for_fallback.update({
                    "id": lane_id,
                    "name": name,
                    "paths_all": paths_all,
                    "subprocess_paths_all": subprocess_paths_all,
                    "messages": messages,
                    "lane_handoffs": lane_handoffs,
                    "data_io": data_io,
                })

                def _len_list(x): return len(x) if isinstance(x, list) else 0

                main_paths_count = 0
                if isinstance(paths_all, dict):
                    mp = paths_all.get("main_paths") or paths_all.get("paths")
                    if isinstance(mp, list):
                        main_paths_count = len(mp)
                elif isinstance(paths_all, list):
                    main_paths_count = len(paths_all)

                msgs_out_count = _len_list((messages or {}).get("out"))
                msgs_in_count = _len_list((messages or {}).get("in"))
                lane_handoffs_count = _len_list(lane_handoffs)

                reads = data_io.get("reads") if isinstance(data_io, dict) else None
                writes = data_io.get("writes") if isinstance(data_io, dict) else None
                reads_stores = _len_list((reads or {}).get("stores"))
                reads_objects = _len_list((reads or {}).get("objects"))
                writes_stores = _len_list((writes or {}).get("stores"))
                writes_objects = _len_list((writes or {}).get("objects"))
                total_reads = reads_stores + reads_objects
                total_writes = writes_stores + writes_objects
            except Exception as e:
                self.logger.error(f"{PFX}[LANE][CTX] input normalization error: {e}", exc_info=True)
                try:
                    f, s = self._lane_fallback_from_signals(payload_for_fallback)
                    return (f or "").strip(), (s or "").strip()
                except Exception as fe:
                    self.logger.error(f"{PFX}[LANE][CTX] fallback error after normalization failure: {fe}", exc_info=True)
                    return "", ""

            # --- 1) Build prompts (guarded) ----------------------------------------
            try:
                sys_prompt = (
                    "You are a BPMN analyst. Read the structured JSON and write concise, factual text. "
                    "Use exact names from the input. Do not invent entities or steps."
                )
                field_guide = (
                    "You will produce two sections (FULL and SUMMARY) about ONE LANE.\n"
                    "Follow this scaffold exactly:\n"
                    "FULL:\n"
                    "Lane Overview\n"
                    "<one sentence>\n\n"
                    "Lane Flow\n"
                    "<3–4 sentences>\n\n"
                    "Message Exchanges and Data Read/Write\n"
                    "<1–2 sentences>\n\n"
                    "Lane Hand-offs\n"
                    "<one brief sentence or 'None'>\n"
                    "SUMMARY:\n"
                    "<text>\n\n"
                    "Authoring rules:\n"
                    "- Lane Overview: one compressed sentence describing the lane's purpose/scope.\n"
                    "- Lane Flow: derive a readable Start→End storyline from paths_all.main_paths (or .paths). "
                    "  Mention gateways only if meaningful; for subprocess hops, briefly note their role.\n"
                    "- Message/Data: summarize key OUT/IN messages and relevant reads/writes in 1–2 sentences.\n"
                    "- Lane Hand-offs: summarize cross-lane direction; if none, write 'None'.\n"
                    "- Use exact names; do not fabricate entities.\n"
                )
                payload = {
                    "id": lane_id,
                    "name": name,
                    "paths_all": paths_all,
                    "subprocess_paths_all": subprocess_paths_all,
                    "messages": messages,
                    "lane_handoffs": lane_handoffs,
                    "data_io": data_io,
                }
                payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
                user_prompt = f"{field_guide}\nJSON INPUT (structured):\n{payload_json}\n\nReturn text ONLY in the exact scaffold above.\n"
            except Exception as e:
                self.logger.error(f"{PFX}[LANE][CTX] prompt build error: {e}", exc_info=True)
                try:
                    f, s = self._lane_fallback_from_signals(payload_for_fallback)
                    return (f or "").strip(), (s or "").strip()
                except Exception as fe:
                    self.logger.error(f"{PFX}[LANE][CTX] fallback error after prompt build failure: {fe}", exc_info=True)
                    return "", ""

            # --- 2) Start log & config diag (guarded) ------------------------------
            try:
                self.logger.info(
                    f"{PFX}[LANE][CTX] start: name='%s' paths(main=%d) msgs(out=%d,in=%d) io(reads=%d,writes=%d) handoffs=%d",
                    name, main_paths_count, msgs_out_count, msgs_in_count, total_reads, total_writes, lane_handoffs_count
                )
                if hasattr(self, "config"):
                    self.logger.debug(
                        f"{PFX}[LANE][CTX] config: model=%s temp=%s maxF=%s maxS=%s timeout=%s",
                        getattr(self.config, "translation_model", None),
                        getattr(self.config, "temperature", None),
                        getattr(self.config, "max_tokens_full", None),
                        getattr(self.config, "max_tokens_summary", None),
                        getattr(self.config, "timeout", None),
                    )
            except Exception as e:
                self.logger.error(f"{PFX}[LANE][CTX] start logging error: {e}", exc_info=True)

            # --- 3) Fallback when no client ----------------------------------------
            try:
                if getattr(self, "client", None) is None:
                    self.logger.warning(f"{PFX}[LANE][CTX] no client configured; using fallback generation.")
                    f, s = self._lane_fallback_from_signals(payload)
                    f, s = (f or "").strip(), (s or "").strip()
                    self.logger.info(f"{PFX}[LANE][CTX] done(fallback): full_chars=%d summary_chars=%d", len(f), len(s))
                    return f, s
            except Exception as e:
                self.logger.error(f"{PFX}[LANE][CTX] fallback path error (no-client): {e}", exc_info=True)
                return "", ""

            # --- 4) API call (guarded) ---------------------------------------------
            try:
                resp = self.client.chat.completions.create(
                    model=getattr(self.config, "translation_model", None),
                    temperature=getattr(self.config, "temperature", 0.2),
                    max_tokens=(getattr(self.config, "max_tokens_full", 800) +
                                getattr(self.config, "max_tokens_summary", 200) + 200),
                    timeout=getattr(self.config, "timeout", 60),
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
            except Exception as e:
                self.logger.error(f"{PFX}[LANE][CTX] API call error: {e}", exc_info=True)
                try:
                    f, s = self._lane_fallback_from_signals(payload)
                    f, s = (f or "").strip(), (s or "").strip()
                    self.logger.info(f"{PFX}[LANE][CTX] done(fallback-after-api-error): full_chars=%d summary_chars=%d", len(f), len(s))
                    return f, s
                except Exception as fe:
                    self.logger.error(f"{PFX}[LANE][CTX] fallback error after API call failure: {fe}", exc_info=True)
                    return "", ""

            # --- 5) Parse response (guarded) ---------------------------------------
            try:
                txt = resp.choices[0].message.content if (resp and getattr(resp, "choices", None)) else ""
            except Exception as e:
                self.logger.error(f"{PFX}[LANE][CTX] response parse error: {e}", exc_info=True)
                try:
                    f, s = self._lane_fallback_from_signals(payload)
                    return (f or "").strip(), (s or "").strip()
                except Exception as fe:
                    self.logger.error(f"{PFX}[LANE][CTX] fallback error after response parse failure: {fe}", exc_info=True)
                    return "", ""

            # --- 6) Extract FULL/SUMMARY (guarded) ---------------------------------
            try:
                full, summary = "", ""
                if isinstance(txt, str) and txt.strip():
                    raw = txt.strip()
                    cleaned = re.sub(r"^```(?:[a-zA-Z0-9_-]+)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
                    m = re.search(r"FULL\s*:\s*(.*?)\s*(?:SUMMARY\s*:\s*(.*))?\s*$",
                                  cleaned, flags=re.IGNORECASE | re.DOTALL)
                    if m:
                        full = (m.group(1) or "").strip()
                        summary = (m.group(2) or "").strip()
                        if not full:
                            self.logger.warning(f"{PFX}[LANE][CTX] FULL parsed empty.")
                        if not summary:
                            self.logger.warning(f"{PFX}[LANE][CTX] SUMMARY parsed empty.")
                    else:
                        self.logger.warning(f"{PFX}[LANE][CTX] 'FULL/SUMMARY' markers not found; using entire content as FULL.")
                        full = cleaned
                else:
                    self.logger.warning(f"{PFX}[LANE][CTX] empty content from model; using empty outputs.")
            except Exception as e:
                self.logger.error(f"{PFX}[LANE][CTX] block parsing error: {e}", exc_info=True)
                full, summary = "", ""

            # --- 7) Finish log & return --------------------------------------------
            try:
                self.logger.debug(f"{PFX}[LANE][CTX] parse: full_chars=%d summary_chars=%d", len(full), len(summary))
                self.logger.info(f"{PFX}[LANE][CTX] done: full_chars=%d summary_chars=%d", len(full), len(summary))
            except Exception as e:
                self.logger.error(f"{PFX}[LANE][CTX] finish logging error: {e}", exc_info=True)

            return (full or "").strip(), (summary or "").strip()

        except Exception as e:
            self.logger.error(f"{PFX}[LANE][CTX] unexpected fatal error: {e}", exc_info=True)
            try:
                f, s = self._lane_fallback_from_signals(payload_for_fallback)
                return (f or "").strip(), (s or "").strip()
            except Exception as fe:
                self.logger.error(f"{PFX}[LANE][CTX] final fallback error after fatal: {fe}", exc_info=True)
                return full_safe, summary_safe

    def _lane_fallback_from_signals(self, payload: dict) -> tuple[str, str]:
        """
        Heuristic fallback summarizer for lane-level signals.
        Keeps output compatible with the expected scaffold semantics.
        """
        try:
            name = payload.get("name") or "This lane"
            nodes = payload.get("nodes_catalog") or {}
            paths_all = payload.get("paths_all") or {}
            sp_all = payload.get("subprocess_paths_all") or []
            messages = payload.get("messages") or {}
            data_io = payload.get("data_io") or {}

            # Extract a main path
            if isinstance(paths_all, dict):
                main_paths = paths_all.get("main_paths") or paths_all.get("paths") or []
            elif isinstance(paths_all, list):
                main_paths = paths_all
            else:
                main_paths = []
            main = (main_paths[0] if main_paths else []) or []

            def _safe(h, side, key):
                try:
                    return (h.get(side) or {}).get(key)
                except Exception:
                    return None

            start_name = _safe(main[0], "source", "name") if main else None
            end_name = _safe(main[-1], "target", "name") if main else None

            # Count activity & gateway/event hints
            from collections import Counter
            act_cnt = Counter()
            gw_dirs, ev_kinds = set(), set()

            def _push_node_meta(nid):
                meta = nodes.get(nid) or {}
                kind = (meta.get("kind") or meta.get("type") or "").lower()
                if "gateway" in kind:
                    d = (meta.get("gateway") or {}).get("direction")
                    if d: gw_dirs.add(str(d).upper())
                elif "event" in kind:
                    det = (meta.get("event") or {}).get("detail")
                    if det: ev_kinds.add(str(det).lower())
                else:
                    nm = meta.get("name")
                    if nm: act_cnt[nm] += 1

            if main:
                for hop in main:
                    _push_node_meta(hop.get("source", {}).get("id"))
                    _push_node_meta(hop.get("target", {}).get("id"))
            else:
                for nid in nodes.keys():
                    _push_node_meta(nid)

            core_steps = [nm for nm, _ in act_cnt.most_common(4)]

            # Messages
            out_msgs = messages.get("out") or []
            in_msgs = messages.get("in") or []

            def _fmt_msg_examples():
                segs = []
                if out_msgs:
                    ex = ", ".join(
                        f"'{m.get('topic')}' to {m.get('to_participant','Unknown')} (node '{m.get('to_node','Unknown')}')"
                        for m in out_msgs[:2] if m.get("topic")
                    )
                    if ex: segs.append(f"It sends business messages such as {ex}.")
                if in_msgs:
                    ex = ", ".join(
                        f"'{m.get('topic')}' from {m.get('from_participant','Unknown')} (node '{m.get('from_node','Unknown')}')"
                        for m in in_msgs[:2] if m.get("topic")
                    )
                    if ex: segs.append(f"It receives messages such as {ex}.")
                return " ".join(segs)
            msg_line = _fmt_msg_examples()

            # Data I/O
            def _collect_names(bucket):
                names = []
                for x in (bucket or []):
                    if isinstance(x, str):
                        names.append(x)
                    elif isinstance(x, dict):
                        label = x.get("data_label") or x.get("dataName") or x.get("dataRefName")
                        if label: names.append(label)
                return names

            r = data_io.get("reads") or {}
            w = data_io.get("writes") or {}
            rnames = _collect_names(r.get("stores")) + _collect_names(r.get("objects"))
            wnames = _collect_names(w.get("stores")) + _collect_names(w.get("objects"))

            dio_line = ""
            segs = []
            if rnames: segs.append("reads " + ", ".join(f"'{x}'" for x in rnames[:5]))
            if wnames: segs.append("writes " + ", ".join(f"'{x}'" for x in wnames[:5]))
            if segs: dio_line = "It " + " and ".join(segs) + "."

            # Subprocess brief
            sp_line = ""
            if isinstance(sp_all, list) and sp_all:
                first_sp = sp_all[0] or {}
                inner_paths = first_sp.get("subprocess_paths") or []
                if inner_paths:
                    inner = inner_paths[0] or []
                    inner_start = _safe(inner[0], "source", "name") if inner else None
                    inner_end = _safe(inner[-1], "target", "name") if inner else None
                    sp_line = (
                        f"It also uses an inner subprocess flow from '{inner_start}' to '{inner_end}'."
                        if inner_start and inner_end else
                        "It also uses inner subprocess flows where relevant."
                    )

            gw_txt = "Decisions are governed by gateways." if gw_dirs else ""
            ev_txt = f"It handles events such as {', '.join(sorted(ev_kinds))}." if ev_kinds else ""

            lines = []
            if start_name and end_name:
                lines.append(f"The '{name}' lane typically begins at '{start_name}' and completes at '{end_name}'.")
            else:
                lines.append(f"The '{name}' lane coordinates its activities from initiation to completion.")
            if core_steps:
                lines.append("Work usually progresses through steps such as " + ", ".join(f"'{x}'" for x in core_steps[:4]) + ".")
            if gw_txt or ev_txt:
                lines.append(" ".join([s for s in [gw_txt, ev_txt] if s]))
            if dio_line:
                lines.append(dio_line)
            if msg_line:
                lines.append(msg_line)
            if sp_line:
                lines.append(sp_line)

            if len(lines) > 5:
                lines = lines[:5]
            elif len(lines) < 4:
                lines.append("It maintains clear interfaces with participants and systems while ensuring data integrity.")

            full = " ".join(lines).strip()
            summary = (
                f"'{name}' focuses on its core activities, applying decisions at gateways and collaborating via targeted messages "
                "while reading and writing key data."
            )
            return full, summary

        except Exception as e:
            self.logger.exception(f"{PFX}[LANE][FALLBACK] summarizer failed: {e}")
            return "", ""

    def generate_process_context(self, signals: dict, larts: Dict[str, Dict[str, Any]] | None = None) -> tuple[str, str]:
        """
        Generate process-level FULL/SUMMARY texts from simplified 'signals', optionally enriched by lane artifacts (larts).
        Enforces 'FULL:' / 'SUMMARY:' markers; falls back heuristically on failure.
        """
        full_safe, summary_safe = "", ""
        payload_for_fallback = {"name": None, "paths_all": None, "subprocess_paths_all": None,
                                "messages": {}, "data_io": {}, "inter_lane_handoffs": None}

        try:
            import json

            # --- 0) Normalize (guarded) --------------------------------------------
            try:
                name = (signals or {}).get("name")
                paths_all = (signals or {}).get("paths_all")
                subprocess_paths_all = (signals or {}).get("subprocess_paths_all")
                messages = (signals or {}).get("messages") or {}
                data_io = (signals or {}).get("data_io") or {}
                inter_lane_handoffs = (signals or {}).get("inter_lane_handoffs")

                payload_for_fallback.update({
                    "name": name,
                    "paths_all": paths_all,
                    "subprocess_paths_all": subprocess_paths_all,
                    "messages": messages,
                    "data_io": data_io,
                    "inter_lane_handoffs": inter_lane_handoffs,
                })

                main_paths_count = 0
                if isinstance(paths_all, dict):
                    mp = paths_all.get("main_paths")
                    if isinstance(mp, list):
                        main_paths_count = len(mp)

                subproc_count = len(subprocess_paths_all) if isinstance(subprocess_paths_all, list) else 0
                msgs_out_count = len(messages.get("out") or []) if isinstance(messages, dict) else 0
                msgs_in_count = len(messages.get("in") or []) if isinstance(messages, dict) else 0
                handoffs_count = len(inter_lane_handoffs) if isinstance(inter_lane_handoffs, list) else 0

                reads = data_io.get("reads") if isinstance(data_io, dict) else None
                writes = data_io.get("writes") if isinstance(data_io, dict) else None
                reads_stores = len((reads or {}).get("stores") or []) if isinstance(reads, dict) else 0
                reads_objects = len((reads or {}).get("objects") or []) if isinstance(reads, dict) else 0
                writes_stores = len((writes or {}).get("stores") or []) if isinstance(writes, dict) else 0
                writes_objects = len((writes or {}).get("objects") or []) if isinstance(writes, dict) else 0
                total_reads = reads_stores + reads_objects
                total_writes = writes_stores + writes_objects
            except Exception as e:
                self.logger.error(f"{PFX}[PROC] input normalization error: {e}", exc_info=True)
                try:
                    f, s = self._process_fallback_from_signals(payload_for_fallback)
                    return (f or "").strip(), (s or "").strip()
                except Exception as fe:
                    self.logger.error(f"{PFX}[PROC] fallback error after normalization failure: {fe}", exc_info=True)
                    return "", ""

            # --- 1) Build prompts (guarded) ----------------------------------------
            try:
                sys_prompt = (
                    "You are a BPMN analyst. Read the structured JSON and write concise, factual text. "
                    "Use exact names from the input. Do not invent entities or steps."
                )
                payload = dict(payload_for_fallback)
                payload_json = json.dumps(payload, ensure_ascii=False, indent=2)

                user_prompt = (
                    "Write two outputs about the PROCESS below. Follow the exact section layout.\n\n"
                    "FULL:\n"
                    "- Compose the FULL output in four sections (use the exact headings and blank lines shown later):\n"
                    "  1) Process Overview — exactly one sentence.\n"
                    "  2) Process Flow — 3–4 sentences narrating Start→End using paths_all.main_paths; "
                    "     for subprocess hops, briefly clarify inner intent using subprocess_paths_all; "
                    "     if larts are provided, mention at most 1–2 lanes owning key steps.\n"
                    "  3) Message Exchanges and Data Read/Write — 1–2 sentences on essential messages and data I/O.\n"
                    "  4) Lane Hand-offs — summarize cross-lane handoffs or write 'None'.\n"
                    "SUMMARY:\n"
                    "- 1–2 sentences: high-level purpose and key interactions.\n\n"
                    "JSON INPUT:\n"
                    f"{payload_json}\n\n"
                    f"LARTS INPUT (array; optional):\n{larts}\n\n"
                    "Output format exactly:\n"
                    "FULL:\n"
                    "Process Overview\n"
                    "<one sentence>\n\n"
                    "Process Flow\n"
                    "<3–4 sentences>\n\n"
                    "Message Exchanges and Data Read/Write\n"
                    "<1–2 sentences>\n\n"
                    "Lane Hand-offs\n"
                    "<one brief sentence or 'None'>\n"
                    "SUMMARY:\n"
                    "<text>\n"
                )
            except Exception as e:
                self.logger.error(f"{PFX}[PROC] prompt build error: {e}", exc_info=True)
                try:
                    f, s = self._process_fallback_from_signals(payload_for_fallback)
                    return (f or "").strip(), (s or "").strip()
                except Exception as fe:
                    self.logger.error(f"{PFX}[PROC] fallback error after prompt build failure: {fe}", exc_info=True)
                    return "", ""

            # --- 2) Start log & config diag ----------------------------------------
            try:
                self.logger.info(
                    f"{PFX}[PROC] start: name='%s' paths(main=%d) subproc=%d msgs(out=%d,in=%d) io(reads=%d,writes=%d) handoffs=%d",
                    payload.get("name"), main_paths_count, subproc_count, msgs_out_count, msgs_in_count,
                    total_reads, total_writes, handoffs_count
                )
                if hasattr(self, "config"):
                    self.logger.debug(
                        f"{PFX}[PROC] config: model=%s temp=%s maxF=%s maxS=%s timeout=%s",
                        getattr(self.config, "translation_model", None),
                        getattr(self.config, "temperature", None),
                        getattr(self.config, "max_tokens_full", None),
                        getattr(self.config, "max_tokens_summary", None),
                        getattr(self.config, "timeout", None),
                    )
            except Exception as e:
                self.logger.error(f"{PFX}[PROC] start logging error: {e}", exc_info=True)

            # --- 3) Fallback when no client ----------------------------------------
            try:
                if getattr(self, "client", None) is None:
                    self.logger.warning(f"{PFX}[PROC] no client configured; using fallback generation.")
                    f, s = self._process_fallback_from_signals(payload)
                    f, s = (f or "").strip(), (s or "").strip()
                    self.logger.info(f"{PFX}[PROC] done(fallback): full_chars=%d summary_chars=%d", len(f), len(s))
                    return f, s
            except Exception as e:
                self.logger.error(f"{PFX}[PROC] fallback path error (no-client): {e}", exc_info=True)
                return "", ""

            # --- 4) API call (guarded) ---------------------------------------------
            try:
                resp = self.client.chat.completions.create(
                    model=getattr(self.config, "translation_model", None),
                    temperature=getattr(self.config, "temperature", 0.2),
                    max_tokens=(getattr(self.config, "max_tokens_full", 800) +
                                getattr(self.config, "max_tokens_summary", 200) + 200),
                    timeout=getattr(self.config, "timeout", 60),
                    messages=[{"role": "system", "content": sys_prompt},
                              {"role": "user", "content": user_prompt}],
                )
            except Exception as e:
                self.logger.error(f"{PFX}[PROC] API call error: {e}", exc_info=True)
                try:
                    f, s = self._process_fallback_from_signals(payload)
                    f, s = (f or "").strip(), (s or "").strip()
                    self.logger.info(f"{PFX}[PROC] done(fallback-after-api-error): full_chars=%d summary_chars=%d", len(f), len(s))
                    return f, s
                except Exception as fe:
                    self.logger.error(f"{PFX}[PROC] fallback error after API call failure: {fe}", exc_info=True)
                    return "", ""

            # --- 5) Parse response (guarded) ---------------------------------------
            try:
                txt = resp.choices[0].message.content if (resp and getattr(resp, "choices", None)) else ""
            except Exception as e:
                self.logger.error(f"{PFX}[PROC] response parse error: {e}", exc_info=True)
                try:
                    f, s = self._process_fallback_from_signals(payload)
                    return (f or "").strip(), (s or "").strip()
                except Exception as fe:
                    self.logger.error(f"{PFX}[PROC] fallback error after response parse failure: {fe}", exc_info=True)
                    return "", ""

            # --- 6) Extract FULL/SUMMARY (guarded) ---------------------------------
            try:
                full, summary = "", ""
                if isinstance(txt, str) and txt.strip():
                    raw = txt.strip()
                    cleaned = re.sub(r"^```(?:[a-zA-Z0-9_-]+)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
                    m = re.search(r"FULL\s*:\s*(.*?)\s*(?:SUMMARY\s*:\s*(.*))?\s*$",
                                  cleaned, flags=re.IGNORECASE | re.DOTALL)
                    if m:
                        full = (m.group(1) or "").strip()
                        summary = (m.group(2) or "").strip()
                        if not full:
                            self.logger.warning(f"{PFX}[PROC] FULL parsed empty.")
                        if not summary:
                            self.logger.warning(f"{PFX}[PROC] SUMMARY parsed empty.")
                    else:
                        self.logger.warning(f"{PFX}[PROC] 'FULL/SUMMARY' markers not found; using entire content as FULL.")
                        full = cleaned
                else:
                    self.logger.warning(f"{PFX}[PROC] empty content from model; using empty outputs.")
            except Exception as e:
                self.logger.error(f"{PFX}[PROC] block parsing error: {e}", exc_info=True)
                full, summary = "", ""

            # --- 7) Finish log & return --------------------------------------------
            try:
                self.logger.debug(f"{PFX}[PROC] parse: full_chars=%d summary_chars=%d", len(full), len(summary))
                self.logger.info(f"{PFX}[PROC] done: full_chars=%d summary_chars=%d", len(full), len(summary))
            except Exception as e:
                self.logger.error(f"{PFX}[PROC] finish logging error: {e}", exc_info=True)

            return full, summary

        except Exception as e:
            self.logger.error(f"{PFX}[PROC] unexpected fatal error: {e}", exc_info=True)
            try:
                f, s = self._process_fallback_from_signals(payload_for_fallback)
                return (f or "").strip(), (s or "").strip()
            except Exception as fe:
                self.logger.error(f"{PFX}[PROC] final fallback error after fatal: {fe}", exc_info=True)
                return full_safe, summary_safe

    def _process_fallback_from_signals(self, payload: dict) -> tuple[str, str]:
        """
        Heuristic fallback summarizer for process-level signals.
        Produces concise FULL/SUMMARY without external calls.
        """
        name = payload.get("name") or "This process"

        # Main path spine
        paths_all = payload.get("paths_all") or {}
        main_paths = paths_all.get("main_paths") if isinstance(paths_all, dict) else None
        main = (main_paths[0] if isinstance(main_paths, list) and main_paths else []) or []

        def _safe(h, side, key):
            try:
                return (h.get(side) or {}).get(key)
            except Exception:
                return None

        start_name = _safe(main[0], "source", "name") if main else None
        end_name = _safe(main[-1], "target", "name") if main else None

        # Key steps / gateway dirs
        from collections import OrderedDict
        seen_steps = OrderedDict()
        gw_dirs = set()
        for hop in main:
            for side in ("source", "target"):
                node = hop.get(side) or {}
                ntype = (node.get("type") or "").lower()
                if ntype == "activity":
                    nm = node.get("name")
                    if nm and nm not in seen_steps:
                        seen_steps[nm] = True
                elif ntype == "gateway":
                    d = node.get("gatewayDirection")
                    if d:
                        gw_dirs.add(str(d).upper())
        core_steps = list(seen_steps.keys())[:5]
        dir_map = {"EXCLUSIVE": "XOR", "INCLUSIVE": "OR", "PARALLEL": "AND", "EVENTBASED": "event-based"}
        gw_labels = sorted({dir_map.get(d, d) for d in gw_dirs})

        # Messages
        messages = payload.get("messages") or {}
        out_msgs = messages.get("out") or []
        in_msgs = messages.get("in") or []

        def _fmt_msg_examples():
            segs = []
            if out_msgs:
                ex = ", ".join(
                    f"'{m.get('topic')}' to {m.get('to_participant','Unknown')} (node '{m.get('to_node','Unknown')}')"
                    for m in out_msgs[:2] if m.get("topic")
                )
                if ex: segs.append(f"It sends business messages such as {ex}.")
            if in_msgs:
                ex = ", ".join(
                    f"'{m.get('topic')}' from {m.get('from_participant','Unknown')} (node '{m.get('from_node','Unknown')}')"
                    for m in in_msgs[:2] if m.get("topic")
                )
                if ex: segs.append(f"It receives messages such as {ex}.")
            return " ".join(segs)

        msg_line = _fmt_msg_examples()

        # Data I/O
        data_io = payload.get("data_io") or {}

        def _fmt_dio():
            r = data_io.get("reads") or {}
            w = data_io.get("writes") or {}
            rnames = (r.get("stores") or []) + (r.get("objects") or [])
            wnames = (w.get("stores") or []) + (w.get("objects") or [])
            segs = []
            if rnames: segs.append("reads " + ", ".join(f"'{x}'" for x in rnames[:5]))
            if wnames: segs.append("writes " + ", ".join(f"'{x}'" for x in wnames[:5]))
            return ("It " + " and ".join(segs) + ".") if segs else ""

        dio_line = _fmt_dio()

        # Inter-lane handoffs
        hand = payload.get("inter_lane_handoffs") or []
        hand_line = ""
        if isinstance(hand, list) and hand:
            pairs = []
            for h in hand[:3]:
                fr = h.get("from_lane", "Lane ?")
                to = h.get("to_lane", "Lane ?")
                cnt = h.get("count", 1)
                pairs.append(f"{fr}→{to}({cnt})")
            if pairs:
                hand_line = "Work hands off across lanes such as " + ", ".join(pairs) + "."

        # Subprocess inner flows
        sp_all = payload.get("subprocess_paths_all") or []
        sp_line = ""
        if isinstance(sp_all, list) and sp_all:
            first_sp = sp_all[0] or {}
            inner_paths = first_sp.get("subprocess_paths") or []
            if inner_paths:
                inner = inner_paths[0] or []
                inner_start = _safe(inner[0], "source", "name") if inner else None
                inner_end = _safe(inner[-1], "target", "name") if inner else None
                sp_line = (
                    f"It also contains an inner subprocess flow from '{inner_start}' to '{inner_end}'."
                    if inner_start and inner_end else
                    "It also contains inner subprocess flows."
                )

        lines = []
        lines.append(f"The '{name}' process orchestrates work from initiation to completion to deliver its intended outcome.")
        if start_name and end_name:
            lines.append(f"It typically begins at '{start_name}' and completes at '{end_name}'.")
        if core_steps:
            lines.append("Key steps include " + ", ".join(f"'{x}'" for x in core_steps) + ".")
        if gw_labels:
            lines.append("Decisions are applied via " + "/".join(gw_labels) + " gateways where relevant.")
        if hand_line:
            lines.append(hand_line)
        if dio_line:
            lines.append(dio_line)
        if msg_line:
            lines.append(msg_line)
        if sp_line:
            lines.append(sp_line)

        if len(lines) > 6:
            lines = lines[:6]
        elif len(lines) < 5:
            lines.append("It maintains clear interfaces with participants and systems while ensuring data integrity.")

        full = " ".join(lines).strip()
        summary = (
            f"'{name}' executes an end-to-end flow with key decisions, lane hand-offs where needed, "
            "essential data reads/writes, and targeted message exchanges; inner subprocess flows may further structure execution."
        )
        return full, summary

    def generate_participant_context(self, participant: dict, processes: list[dict]) -> tuple[str, str]:
        """
        Generate participant-level FULL/SUMMARY texts from participant + processes.
        Enforces 'FULL:' / 'SUMMARY:' markers; relies on caller for fallback.
        """
        import json

        full, summary = "", ""

        try:
            pname = (participant or {}).get("name") or "Participant"
            pid = (participant or {}).get("id")

            proc_payload = []
            for p in (processes or []):
                proc_payload.append({
                    "id": str(p.get("id") or "").strip(),
                    "name": (p.get("name") or "").strip(),
                    "full_text": (p.get("full_text") or "").strip(),
                    "summary_text": (p.get("summary_text") or "").strip(),
                })

            self.logger.info(f"{PFX}[PARTICIPANT] start: pid=%s name='%s' processes=%d", pid, pname, len(proc_payload))
            self.logger.debug(f"{PFX}[PARTICIPANT] payload keys=%s", (list(proc_payload[0].keys()) if proc_payload else []))

            if getattr(self, "client", None) is None:
                self.logger.warning(f"{PFX}[PARTICIPANT] no client configured; returning empty for fallback.")
                return "", ""

            sys_prompt = (
                "You are a BPMN analyst. You receive a participant and its processes. "
                "Write concise, factual text using ONLY provided names. Do not invent entities or steps."
            )
            user_prompt = (
                "Write two blocks based on the JSON.\n\n"
                "FULL:\n"
                f"- First, compress the overall role of the participant '{pname}' in <= 2 sentences (must include the participant name).\n"
                "- Then for each process, write its role in <= 4 sentences, using the process name as-is.\n\n"
                "SUMMARY:\n"
                f"- Only the participant-level role for '{pname}' in 1–2 sentences.\n\n"
                "JSON INPUT (structured):\n"
                + json.dumps({"participant": participant, "processes": proc_payload}, ensure_ascii=False, indent=2)
                + "\n\nOutput format exactly:\nFULL:\n<text>\nSUMMARY:\n<text>\n"
            )

            self.logger.debug(
                f"{PFX}[PARTICIPANT] LLM call: model=%s temp=%s maxF=%s maxS=%s timeout=%s",
                getattr(self.config, "translation_model", None),
                getattr(self.config, "temperature", None),
                getattr(self.config, "max_tokens_full", None),
                getattr(self.config, "max_tokens_summary", None),
                getattr(self.config, "timeout", None),
            )

            resp = self.client.chat.completions.create(
                model=getattr(self.config, "translation_model", None),
                temperature=getattr(self.config, "temperature", 0.2),
                max_tokens=(getattr(self.config, "max_tokens_full", 800) +
                            getattr(self.config, "max_tokens_summary", 200) + 200),
                timeout=getattr(self.config, "timeout", 60),
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )

            txt = ""
            try:
                txt = resp.choices[0].message.content if (resp and getattr(resp, "choices", None)) else ""
            except Exception as pe:
                self.logger.error(f"{PFX}[PARTICIPANT] response parse error: {pe}", exc_info=True)

            self.logger.info(f"{PFX}[PARTICIPANT] llm response txt: %s", txt)
            if isinstance(txt, str) and txt:
                upper = txt.upper()
                if "FULL:" in upper:
                    _, _, rest = txt.partition("FULL:")
                    if "SUMMARY:" in rest:
                        full, _, summary = rest.partition("SUMMARY:")
                    else:
                        full = rest
                else:
                    self.logger.warning(f"{PFX}[PARTICIPANT] 'FULL:' marker not found; using entire content as FULL.")
                    full = txt
            else:
                self.logger.warning(f"{PFX}[PARTICIPANT] empty content from model; returning empty for fallback.")
                return "", ""

            full, summary = (full or "").strip(), (summary or "").strip()

            self.logger.info(f"{PFX}[PARTICIPANT] done: full_chars=%d summary_chars=%d", len(full or ""), len(summary or ""))
            self.logger.debug(f"{PFX}[PARTICIPANT] FULL (trunc): %s", full if len(full) <= 2000 else (full[:2000] + "..."))
            self.logger.debug(f"{PFX}[PARTICIPANT] SUMMARY: %s", summary)

            return full, summary

        except Exception as e:
            self.logger.error(f"{PFX}[PARTICIPANT] fatal error: {e}", exc_info=True)
            return "", ""

    def generate_model_context(self, model: dict, participants: list[dict]) -> tuple[str, str]:
        """
        Generate model-level FULL/SUMMARY texts from model + participants.
        Enforces 'FULL:' / 'SUMMARY:' markers; relies on caller for fallback.
        """
        import json, re

        full, summary = "", ""

        try:
            # 1) Normalize inputs (guarded) -----------------------------------------
            try:
                mname = (model or {}).get("name") or "Model"
                mid = (model or {}).get("id")

                part_payload = []
                for p in (participants or []):
                    part_payload.append({
                        "id": str(p.get("id") or "").strip(),
                        "name": (p.get("name") or "").strip(),
                        "full_text": (p.get("full_text") or "").strip(),
                        "summary_text": (p.get("summary_text") or "").strip(),
                    })

                self.logger.info(f"{PFX}[MODEL] start: mid=%s name='%s' participants=%d", mid, mname, len(part_payload))
                self.logger.debug(f"{PFX}[MODEL] payload keys=%s", (list(part_payload[0].keys()) if part_payload else []))
            except Exception as e_norm:
                self.logger.error(f"{PFX}[MODEL] normalization error: {e_norm}", exc_info=True)
                return "", ""

            # 2) Client presence check ----------------------------------------------
            try:
                if getattr(self, "client", None) is None:
                    self.logger.warning(f"{PFX}[MODEL] no client configured; returning empty for fallback.")
                    return "", ""
            except Exception as e_client:
                self.logger.error(f"{PFX}[MODEL] client check error: {e_client}", exc_info=True)
                return "", ""

            # 3) Build prompts (guarded) --------------------------------------------
            try:
                sys_prompt = (
                    "You are a BPMN analyst. You receive a model and its participants (with their aggregated process texts). "
                    "Write concise, factual text using ONLY provided names. Do not invent entities or steps."
                )
                user_prompt = (
                    "Write two blocks based on the JSON.\n\n"
                    "FULL:\n"
                    f"- First, compress the overall role of the model '{mname}' in <= 2 sentences (must include the model name).\n"
                    "- Then for each participant, start a new line with '## {ParticipantName}.' and write its role in <= 4 sentences.\n\n"
                    "SUMMARY:\n"
                    f"- Only the model-level role for '{mname}' in 1–2 sentences.\n\n"
                    "JSON INPUT (structured):\n"
                    + json.dumps({"model": model, "participants": part_payload}, ensure_ascii=False, indent=2)
                    + "\n\nOutput format exactly:\nFULL:\n<text>\nSUMMARY:\n<text>\n"
                )
                self.logger.debug(
                    f"{PFX}[MODEL] LLM call: model=%s temp=%s maxF=%s maxS=%s timeout=%s",
                    getattr(self.config, "translation_model", None),
                    getattr(self.config, "temperature", None),
                    getattr(self.config, "max_tokens_full", None),
                    getattr(self.config, "max_tokens_summary", None),
                    getattr(self.config, "timeout", None),
                )
            except Exception as e_prompt:
                self.logger.error(f"{PFX}[MODEL] prompt build error: {e_prompt}", exc_info=True)
                return "", ""

            # 4) Call LLM (guarded) -------------------------------------------------
            try:
                resp = self.client.chat.completions.create(
                    model=getattr(self.config, "translation_model", None),
                    temperature=getattr(self.config, "temperature", 0.2),
                    max_tokens=(getattr(self.config, "max_tokens_full", 800) +
                                getattr(self.config, "max_tokens_summary", 200) + 200),
                    timeout=getattr(self.config, "timeout", 60),
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
            except Exception as e_api:
                self.logger.error(f"{PFX}[MODEL] API call error: {e_api}", exc_info=True)
                return "", ""

            # 5) Extract text (guarded) ---------------------------------------------
            try:
                txt = resp.choices[0].message.content if (resp and getattr(resp, "choices", None)) else ""
            except Exception as e_parse:
                self.logger.error(f"{PFX}[MODEL] response parse error: {e_parse}", exc_info=True)
                return "", ""

            # 6) Parse FULL/SUMMARY markers (guarded) -------------------------------
            try:
                self.logger.info(f"{PFX}[MODEL] llm response txt: %s", txt)

                raw = txt if isinstance(txt, str) else ""
                cleaned = raw.strip()
                cleaned = re.sub(r"^```(?:[a-zA-Z0-9_-]+)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)

                m = re.search(r"FULL\s*:\s*(.*?)\s*(?:SUMMARY\s*:\s*(.*))?\s*$",
                              cleaned, flags=re.IGNORECASE | re.DOTALL)

                if m:
                    full = (m.group(1) or "").strip()
                    summary = (m.group(2) or "").strip()
                    if not full:
                        self.logger.warning(f"{PFX}[MODEL] FULL parsed empty.")
                    if not summary:
                        self.logger.warning(f"{PFX}[MODEL] SUMMARY parsed empty.")
                else:
                    self.logger.warning(f"{PFX}[MODEL] 'FULL/SUMMARY' markers not found; using entire content as FULL.")
                    full = cleaned
                    summary = ""

                full, summary = (full or "").strip(), (summary or "").strip()
            except Exception as e_block:
                self.logger.error(f"{PFX}[MODEL] block parsing error: {e_block}", exc_info=True)
                return "", ""

            # 7) Final logging & return ---------------------------------------------
            try:
                self.logger.info(f"{PFX}[MODEL] done: full_chars=%d summary_chars=%d", len(full or ""), len(summary or ""))
                self.logger.debug(f"{PFX}[MODEL] FULL (trunc): %s", full if len(full) <= 2000 else (full[:2000] + "..."))
                self.logger.debug(f"{PFX}[MODEL] SUMMARY: %s", summary)
            except Exception as e_fin:
                self.logger.error(f"{PFX}[MODEL] finish logging error: {e_fin}", exc_info=True)

            return full, summary

        except Exception as e_fatal:
            self.logger.error(f"{PFX}[MODEL] fatal error: {e_fatal}", exc_info=True)
            return "", ""
