"""Deterministic, explainable financial sentiment with the Loughran–McDonald (LM) Master Dictionary.

The dictionary is not shipped with optlab. It is free for academic research; commercial use needs a license
from its authors (https://sraf.nd.edu/loughranmcdonald-master-dictionary/). `scripts/news.py
install-lexicon` downloads the official file after you accept those terms, into data/lexicon/.

Scoring rules (lm-rules/v1)
    text      the headline and the summary (provider's own, or the item's opening sentences)
    tokens    words of letters (apostrophes kept), upper-cased, matched against the dictionary
    lists     Positive, Negative, Uncertainty, Litigious, Constraining; a word is in a list when its column
              holds the year it was added (a negative year means it was removed, so it's left out)
    negation  a Positive word preceded within three words by no, not, none, neither, never or nobody counts
              as Negative (Loughran & McDonald 2011)
    score     (positive − negative) ÷ (positive + negative), from −1 to +1; none when neither occurs
    label     positive if score ≥ 0.25, negative if ≤ −0.25, neutral otherwise or when no sentiment word
              occurs; unknown when there are fewer than 3 words to judge
    confidence  hits ÷ (hits + 2), where hits = positive + negative; halved when Uncertainty words are at
              least as many as the hits

Other models can be added behind the same interface (`SentimentModel`) without changing the database or
the API: their results are stored under their own `model` and `model_version`. Any model-based scorer must
be switched on explicitly and is labelled as such.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from ..config import PROJECT_ROOT

LM_FILE = "Loughran-McDonald_MasterDictionary_1993-2025.csv"
LM_URL = "https://drive.usercontent.google.com/download?id=1iq2RUf8qGFEAk1g8wQntP3habOnR3fXF&export=download&confirm=t"
LM_PAGE = "https://sraf.nd.edu/loughranmcdonald-master-dictionary/"
LM_LICENSE = ("The Loughran-McDonald dictionary is free for use in academic research. For commercial licenses, "
              "contact loughranmcdonald@gmail.com (" + LM_PAGE + ").")
DEFAULT_PATH = PROJECT_ROOT / "data" / "lexicon" / LM_FILE
RULES_VERSION = "lm-rules/v1"
CATEGORIES = ("positive", "negative", "uncertainty", "litigious", "constraining")
NEGATORS = frozenset({"NO", "NOT", "NONE", "NEITHER", "NEVER", "NOBODY"})
TOKEN = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
POS_CUT, NEG_CUT, MIN_WORDS = 0.25, -0.25, 3


@dataclass
class SentimentResult:
    model: str
    model_version: str
    label: str
    score: float | None
    confidence: float
    word_count: int
    counts: dict[str, int]
    terms: dict[str, list[str]]
    negated: list[str] = field(default_factory=list)

    def explanation(self) -> str:
        if self.label == "unknown":
            return "Too little text to judge."
        parts = [f"{k}: {', '.join(v[:8]).lower()}" for k, v in self.terms.items() if v]
        if self.negated:
            parts.append(f"negated positives (counted as negative): {', '.join(self.negated[:5]).lower()}")
        base = (f"{self.counts['positive']} positive and {self.counts['negative']} negative Loughran–McDonald "
                f"words in {self.word_count}")
        return base + (" (" + "; ".join(parts) + ")" if parts else "; no sentiment words")


class SentimentModel(Protocol):
    name: str
    version: str

    def score(self, text: str) -> SentimentResult: ...


@dataclass(frozen=True)
class Lexicon:
    lists: dict[str, frozenset[str]]
    name: str
    sha256: str

    @classmethod
    def from_csv(cls, data: bytes, name: str = LM_FILE) -> Lexicon:
        rows = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        cols = {"positive": "Positive", "negative": "Negative", "uncertainty": "Uncertainty",
                "litigious": "Litigious", "constraining": "Constraining"}
        if rows.fieldnames is None or not set(cols.values()) <= set(rows.fieldnames) or "Word" not in rows.fieldnames:
            raise ValueError("not a Loughran–McDonald Master Dictionary CSV (columns Word, Positive, Negative, "
                             "Uncertainty, Litigious, Constraining are required)")
        lists: dict[str, set[str]] = {k: set() for k in cols}
        for r in rows:
            w = (r["Word"] or "").strip().upper()
            for k, c in cols.items():
                v = (r.get(c) or "0").strip()
                try:
                    if int(float(v)) > 0:                 # year added; negative = removed
                        lists[k].add(w)
                except ValueError:
                    continue
        if not lists["positive"] or not lists["negative"]:
            raise ValueError("the dictionary has no positive or negative words")
        return cls({k: frozenset(v) for k, v in lists.items()}, name, hashlib.sha256(data).hexdigest())


class LoughranMcDonald:
    name = "loughran-mcdonald"

    def __init__(self, lex: Lexicon):
        self.lex = lex
        stem = lex.name.rsplit(".", 1)[0].replace("Loughran-McDonald_MasterDictionary_", "lm-")
        self.version = f"{stem}@{lex.sha256[:12]}/{RULES_VERSION}"

    def score(self, text: str) -> SentimentResult:
        words = [w.upper() for w in TOKEN.findall(text or "")]
        terms: dict[str, list[str]] = {k: [] for k in CATEGORIES}
        negated: list[str] = []
        for i, w in enumerate(words):
            for k in CATEGORIES:
                if w in self.lex.lists[k]:
                    if k == "positive" and any(x in NEGATORS for x in words[max(0, i - 3):i]):
                        negated.append(w)
                        terms["negative"].append(w)
                    else:
                        terms[k].append(w)
        counts = {k: len(v) for k, v in terms.items()}
        pos, neg = counts["positive"], counts["negative"]
        hits = pos + neg
        if len(words) < MIN_WORDS:
            return SentimentResult(self.name, self.version, "unknown", None, 0.0, len(words), counts, terms, negated)
        score = None if hits == 0 else round((pos - neg) / hits, 4)
        label = "neutral" if score is None else "positive" if score >= POS_CUT else "negative" if score <= NEG_CUT else "neutral"
        conf = hits / (hits + 2) if hits else 0.0
        if hits and counts["uncertainty"] >= hits:
            conf /= 2
        return SentimentResult(self.name, self.version, label, score, round(conf, 4), len(words), counts, terms, negated)


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> LoughranMcDonald:
    p = Path(path)
    return LoughranMcDonald(Lexicon.from_csv(p.read_bytes(), p.name))


def default_model(path: str | Path | None = None) -> tuple[LoughranMcDonald | None, str | None]:
    """The installed LM model, or (None, why it's unavailable)."""
    p = Path(path or DEFAULT_PATH)
    if not p.exists():
        return None, (f"The Loughran–McDonald dictionary isn't installed ({p}). Run "
                      "`python scripts/news.py install-lexicon --accept-license` (see its terms first).")
    try:
        return _load(str(p), p.stat().st_mtime), None
    except (OSError, ValueError) as exc:
        return None, f"The Loughran–McDonald dictionary at {p} can't be read: {exc}"
