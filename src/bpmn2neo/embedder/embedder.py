from typing import List, Optional
import time
import math

from openai import OpenAI

from bpmn2neo.config.logger import Logger
from bpmn2neo.settings import OpenAISettings

class Embedder:
    
    """
    Efficient embedding helper using OpenAI models with:
      - Batch processing
      - Exponential backoff on rate limits
      - Optional L2 normalization
      - Bi-directional translation (KO↔EN) with term preservation
      - Optional original+translated combination for embeddings

    Models / Dimensions:
      - EMBED_MODEL = "text-embedding-3-large" (dim=3072)
      - TRANSLATE_MODEL = "gpt-4o-mini"
    """

    def __init__(
        self,
        openai_config: OpenAISettings
    ):
        """
        Args:
            api_client: OpenAI client (OpenAI(api_key=...)) or None for lazy init.
            embed_model: embedding model name.
            embed_dim: expected vector dimension (3072 for '3-large').
            translate_model: chat model for KO↔EN translation.
            batch_size: texts per API call.
            l2_normalize: L2-normalize vectors.
            translate_direction: 'none' | 'ko2en' | 'en2ko'.
            combine_translated: if True, embed original + translated together.
            combine_separator: separator between original and translated.
            preserve_terms: terms to preserve verbatim during translation.
            auto_translate_to_en: legacy flag; treated as translate_direction='ko2en' if True.
            max_retries / initial_backoff: exponential backoff controls.
        """
        self.client = OpenAI(api_key=openai_config.api_key) if openai_config.api_key else OpenAI()

        # --- internal constants (kept simple & self-contained) ---
        self.EMBED_MODEL = openai_config.embedding_model
        self.EMBED_DIM = openai_config.embedding_dimension
        self.TRANSLATE_MODEL = openai_config.translation_model
        self.TRANSLATE_DIRECTION = "en2ko"   # default pipeline policy
        self.COMBINE_TRANSLATED = True       # "EN || KO"
        self.COMBINE_SEPARATOR = " || "
        self.PRESERVE_TERMS = ["BPMN", "Neo4j", "CallActivity", "SubProcess"]
        self.BATCH_SIZE = 64
        self.L2_NORMALIZE = True
        self.MAX_RETRIES = 5
        self.INITIAL_BACKOFF = 0.5

        self.logger = Logger.get_logger(self.__class__.__name__)

        self.logger.info(
            "[EMBED] init: model=%s dim=%s translate=%s combine=%s l2=%s batch=%s",
            self.EMBED_MODEL, self.EMBED_DIM, self.TRANSLATE_DIRECTION,
            self.COMBINE_TRANSLATED, self.L2_NORMALIZE, self.BATCH_SIZE
        )

    # ---------------- Public API ----------------
    def embed(self, text: str) -> List[float]:
        """Single-text embedding API -> List[float]."""
        vecs = self.embed_many([text])
        return vecs[0] if vecs else []

    def embed_many(self, texts: List[str]) -> List[List[float]]:
        """
        Batch embedding:
          1) Light pre-normalization.
          2) Optional translation (KO↔EN) per translate_direction.
          3) Optional combination: "original + separator + translated".
          4) Embedding with exponential backoff.
          5) Optional L2 normalization.
        """
        if not texts:
            return []

        # 1) pre-normalize
        originals = [self._pre_embed_normalize(t) for t in texts]

        # 2) translate if requested
        translated = None
        if self.TRANSLATE_DIRECTION == "en2ko":
            translated = self._translate_batch_en_to_ko(originals)

        # 3) choose final strings to embed
        if translated is not None:
            if self.COMBINE_TRANSLATED:
                final_texts = [
                    self._combine_original_translated(o, tr, self.COMBINE_SEPARATOR)
                    for o, tr in zip(originals, translated)
                ]
            else:
                final_texts = translated
        else:
            final_texts = originals

        # 4) embed (batched with backoff)
        out: List[List[float]] = []
        for off in range(0, len(final_texts), self.BATCH_SIZE):
            batch = final_texts[off : off + self.BATCH_SIZE]
            vecs = self._embed_with_backoff(batch)
            out.extend(vecs)

        # 5) L2 normalize (optional)
        if self.L2_NORMALIZE:
            out = [self._l2_normalize(v) for v in out]

        # ensure python list of floats
        return [[float(x) for x in v] for v in out]

    # --------------- Internals ---------------

    def _embed_with_backoff(self, batch: List[str]) -> List[List[float]]:
        if self.client is None:
            raise RuntimeError("OpenAI client is not initialized")

        delay = self.INITIAL_BACKOFF
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                resp = self.client.embeddings.create(
                    model=self.EMBED_MODEL,
                    input=batch,
                    encoding_format="float",
                )
                vecs = [d.embedding for d in resp.data]
                if vecs and len(vecs[0]) != self.EMBED_DIM:
                    self.logger.warning("[03.EMBED] dim-mismatch: expected=%s got=%s", self.EMBED_DIM, len(vecs[0]))
                return vecs
            except Exception as e:
                if attempt == self.MAX_RETRIES:
                    self.logger.exception("[03.EMBED] failed after %s attempts: %s", attempt, e)
                    raise
                sleep_for = delay * (1.0 + 0.25 * (attempt % 3))
                self.logger.warning("[03.EMBED] retry %s/%s after error: %s (sleep=%.2fs)", attempt, self.MAX_RETRIES, e, sleep_for)
                time.sleep(sleep_for)
                delay *= 2

    # ---------- Translation (KO↔EN) ----------

    def _translate_batch_ko_to_en(self, texts: List[str]) -> List[str]:
        """Korean → English (term-preserving)."""
        if not texts:
            return []
        if self.client is None:
            self.logger.warning("[EMBED] KO→EN translation skipped: no client")
            return texts
        out: List[str] = []
        for t in texts:
            # Heuristic: if already English-dominant, skip
            if self._looks_english(t):
                out.append(t)
            else:
                out.append(self._translate_one(t, src="ko", tgt="en"))
        return out

    def _translate_batch_en_to_ko(self, texts: List[str]) -> List[str]:
        """English → Korean (term-preserving)."""
        if not texts:
            return []
        if self.client is None:
            self.logger.warning("[EMBED] EN→KO translation skipped: no client")
            return texts
        out: List[str] = []
        for t in texts:
            # Heuristic: if Hangul already present (likely KO), skip
            if self._looks_korean(t):
                out.append(t)
            else:
                out.append(self._translate_one(t, src="en", tgt="ko"))
        return out

    def _translate_one(self, text: str, src: str, tgt: str) -> str:
        """Generic translator using gpt-4o-mini with term preservation."""
        guard = self._guard_terms(text, self.PRESERVE_TERMS)
        sys_prompt = (
            "You are a precise technical translator.\n"
            f"Translate {src.upper()} to {tgt.upper()} concisely.\n"
            "- Preserve any terms wrapped in « » EXACTLY as they appear.\n"
            "- Keep IDs, code, keys, and acronyms unchanged.\n"
            "- Do not add explanations; return only the translated text."
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.TRANSLATE_MODEL,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": guard},
                ],
                temperature=0.2,
            )
            out = resp.choices[0].message.content.strip()
            return self._unguard_terms(out)
        except Exception as e:
            self.logger.warning("[EMBED] translation error (%s→%s); fallback to original: %s", src, tgt, e)
            return text
        
    # ------------- Small utilities -------------

    @staticmethod
    def _combine_original_translated(original: str, translated: str, sep: str) -> str:
        """
        Combine original and translated strings into a single embedding text.
        - Avoid duplicates if translation was skipped or equal.
        """
        o = (original or "").strip()
        t = (translated or "").strip()
        if not t or t == o:
            return o
        if not o:
            return t
        return f"{o}{sep}{t}"

    @staticmethod
    def _pre_embed_normalize(text: str) -> str:
        """Light normalization: strip, collapse whitespace, drop control chars (keep ASCII ws)."""
        if text is None:
            return ""
        s = str(text)
        s = "".join(ch if (ch in ("\n", "\t", " ") or ch >= " ") else " " for ch in s)
        s = " ".join(s.split())
        return s.strip()

    @staticmethod
    def _l2_normalize(vec: List[float]) -> List[float]:
        norm = math.sqrt(sum((x * x) for x in vec)) or 1.0
        return [x / norm for x in vec]

    @staticmethod
    def _looks_english(text: str) -> bool:
        latin = sum(1 for c in (text or "") if ("A" <= c <= "Z") or ("a" <= c <= "z"))
        hangul = sum(1 for c in (text or "") if "\uac00" <= c <= "\ud7a3")
        return latin > 0 and hangul == 0

    @staticmethod
    def _looks_korean(text: str) -> bool:
        hangul = sum(1 for c in (text or "") if "\uac00" <= c <= "\ud7a3")
        return hangul > 0

    @staticmethod
    def _guard_terms(text: str, terms: List[str]) -> str:
        if not terms:
            return text
        s = text
        for t in terms:
            if t:
                s = s.replace(t, f"«{t}»")
        return s

    @staticmethod
    def _unguard_terms(text: str) -> str:
        return (text or "").replace("«", "").replace("»", "")
