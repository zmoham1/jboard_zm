"""Data-domain job title classifier — tuned for Data Analyst / Data Scientist / Data Engineer roles.

Scoring:
  yes   (score 70–100) — strong data role match
  maybe (score 40–69)  — data role with seniority or ambiguity; review manually
  no    (score  0–39)  — not a data role (software-only, ops, sales, QA, etc.)

Design rationale
-----------------
Only data-domain roles pass. Pure software engineering, DevOps, QA, security,
PM, sales, and non-data-facing roles all return "no" so the user only sees
Data Analyst, Data Scientist, Data Engineer, ML Engineer, BI Analyst, etc.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .software_keywords import (
    DATA_DOMAIN_EXCLUDES,
    MANAGEMENT_EXCLUDES,
    EARLY_CAREER_SIGNALS,
    SOFTWARE_HARD_EXCLUDES,
    SOFTWARE_STRONG,
    SOFTWARE_WEAK,
)

# ---------------------------------------------------------------------------
# Data-domain STRONG includes  →  base score 90
# ---------------------------------------------------------------------------
DATA_STRONG = [
    # Core data roles
    "data analyst",
    "data analytics",
    "data scientist",
    "data science",
    "data engineer",
    "data engineering",
    "analytics engineer",
    "analytics analyst",
    # Business Intelligence
    "business intelligence",
    "bi analyst",
    "bi engineer",
    "bi developer",
    "bi developer",
    "intelligence analyst",
    # Machine Learning / AI (data side)
    "machine learning engineer",
    "ml engineer",
    "applied scientist",
    "research scientist",
    "decision scientist",
    "ai data",
    # Quantitative / Statistical
    "quantitative analyst",
    "quant analyst",
    "statistical analyst",
    "statistical modeler",
    "forecasting analyst",
    # Platform / Infrastructure / Quality (data)
    "data platform engineer",
    "data infrastructure engineer",
    "data reliability engineer",
    "data quality engineer",
    "data quality analyst",
    "data governance",
    "data management analyst",
    "data operations analyst",
    "data architect",
    "analytics architect",
    # ETL / Warehouse
    "etl engineer",
    "etl developer",
    "elt engineer",
    "data warehouse engineer",
    "data warehousing",
    "dwh engineer",
    "data modeler",
    "data modeling",
    # Insights / Reporting
    "insights analyst",
    "insights engineer",
    "reporting analyst",
    "product analyst",
    "growth analyst",
    "marketing analyst",
    "financial analyst",
    "operations analyst",
    "clinical data analyst",
    "research analyst",
    # AI/ML Data Engineering
    "feature engineer",
    "mlops engineer",
    "ml platform engineer",
    "ai engineer",
    # LLM / Generative AI (modern AI roles)
    "llm engineer",
    "llm data",
    "prompt engineer",
    "generative ai engineer",
    "gen ai engineer",
    "nlp engineer",
    "natural language processing engineer",
    "natural language processing scientist",
    "computer vision engineer",
    "computer vision scientist",
    "multimodal",
    "foundation model",
    # Consulting / advisory (data-focused)
    "analytics consultant",
    "data consultant",
    "data advisor",
]

# ---------------------------------------------------------------------------
# Data-domain WEAK includes  →  base score 55 (needs review)
# ---------------------------------------------------------------------------
DATA_WEAK = [
    "analytics",
    "intelligence",
    "insights",
    "tableau",
    "power bi",
    "snowflake",
    "spark",
    "databricks",
    "data warehouse",
    "pipeline",
    "etl",
    "elt",
    "dbt",
    "airflow",
    "kafka",
    "flink",
    "hadoop",
    # AI/LLM weak signals
    "generative ai",
    "gen ai",
    "large language model",
    "llm",
    "nlp",
    "ai analyst",
    "ai scientist",
    # Business/operations data-adjacent
    "business analyst",
    "business intelligence analyst",
    "operations research",
]

# ---------------------------------------------------------------------------
# Hard excludes — non-data roles → immediate "no"
# ---------------------------------------------------------------------------
HARD_EXCLUDES = [
    # Pure software engineering (no data modifier)
    "software engineer",
    "software developer",
    "software development engineer",
    "frontend engineer",
    "front-end engineer",
    "front end engineer",
    "backend engineer",
    "back-end engineer",
    "back end engineer",
    "full stack engineer",
    "fullstack engineer",
    "full-stack engineer",
    "mobile engineer",
    "ios engineer",
    "android engineer",
    "embedded engineer",
    "embedded software",
    "systems engineer",
    "site reliability",
    "sre",
    "devops",
    "platform engineer",
    "cloud engineer",
    "infrastructure engineer",
    "network engineer",
    "security engineer",
    "cybersecurity",
    "penetration tester",
    # QA / Testing
    "quality assurance",
    "qa engineer",
    "qa analyst",
    "test engineer",
    "quality engineer",
    "validation engineer",
    # Management / non-technical
    "product manager",
    "program manager",
    "project manager",
    "engineering manager",
    "scrum master",
    "agile coach",
    # Sales / Marketing / HR
    "sales",
    "account executive",
    "account manager",
    "solutions engineer",
    "pre-sales",
    "recruiter",
    "talent acquisition",
    "human resources",
    # Support / Ops
    "customer support",
    "technical support",
    "support engineer",
    "help desk",
    "it support",
    "it administrator",
    "systems administrator",
    "sysadmin",
    "database administrator",
    "data entry",
    "data center",
    "accounts payable",
    "billing analyst",
    "claims analyst",
    "procurement analyst",
    "inventory analyst",
    "legal analyst",
    "compliance analyst",
    # Hardware / non-software
    "hardware engineer",
    "electrical engineer",
    "mechanical engineer",
    "manufacturing engineer",
    "supply chain",
]

HARD_EXCLUDE_REGEXES = [
    r"\bintern(ship)?\b",
    r"\bco[- ]?op\b",
    r"\bcoop\b",
    r"\bapprentice\b",
    r"\bpart[- ]time\b",
]

# ---------------------------------------------------------------------------
# Clearance / citizenship filters — ABSOLUTE, cannot be overridden.
# Removes jobs that require security clearance or US citizenship.
# ---------------------------------------------------------------------------
CLEARANCE_EXCLUDE_PHRASES = [
    "security clearance",
    "clearance required",
    "clearance preferred",
    "clearance eligible",
    "active clearance",
    "active secret",
    "secret clearance",
    "top secret",
    "ts/sci",
    "ts sci",
    "sci clearance",
    "dod clearance",
    "dod secret",
    "public trust",
    "polygraph",
    "us citizen",
    "u.s. citizen",
    "must be a citizen",
    "citizenship required",
    "citizenship eligibility",
    "must hold clearance",
]

CLEARANCE_EXCLUDE_REGEXES = [
    r"\bts[/\s\-]?sci\b",       # TS/SCI, TS SCI, TS-SCI
    r"\btop\s+secret\b",         # Top Secret
    r"\bpolygraph\b",            # Polygraph
    r"\bpublic\s+trust\b",       # Public Trust
    r"\bclearance\b",            # any "clearance" in title
    # "Secret cleared", "TS cleared", "must be cleared" — the participle form
    # was missed by the \bclearance\b rule, so titles like
    # "Data Scientist/Application Developer (Secret cleared)" scored as matches.
    r"\bcleared\b",
    r"\bus\s+citizen",           # US citizen / US citizenship
    r"\bcitizenship\b",          # citizenship requirement
    r"\bsci\b",                  # SCI in title (often paired with TS)
]

# ---------------------------------------------------------------------------
# Seniority tokens — always clamp to "maybe" or "no"
# ---------------------------------------------------------------------------
SENIORITY_TOKENS = [
    "senior", "sr", "staff", "principal", "lead", "architect",
    "distinguished", "fellow", "director", "manager", "head of",
    "vp", "vice president",
]
VERY_SENIOR = frozenset(["director", "vp", "vice president", "head of", "fellow", "distinguished"])

# ---------------------------------------------------------------------------
# "data" safety-net: if the title contains "data" AND a hard-excluded term,
# the "data" wins for these specific combos (e.g. "Data Security Analyst")
# ---------------------------------------------------------------------------
DATA_SAFETY_NET_OVERRIDES = frozenset([
    "data security analyst",
    "data quality engineer",
    "data governance",
    "data management",
    "data operations",
    "data steward",
    "data catalog",
    "data platform engineer",
    "data platform",
    "data infrastructure",
    "data reliability engineer",
    # Product/program roles that are genuinely data-focused
    "data product manager",
    "data program manager",
    "analytics program manager",
    # AI roles that may hit SWE-adjacent hard-excludes
    "generative ai",
    "gen ai",
    "llm engineer",
    "prompt engineer",
    "ai data engineer",
])


@dataclass
class ClassifyResult:
    score: int   # 0-100
    label: str   # "yes" | "maybe" | "no"


@lru_cache(maxsize=4096)
def _keyword_pattern(phrase: str) -> "re.Pattern[str]":
    """Word-boundary matcher for a keyword phrase.

    Keywords used to be matched as raw substrings, so short ones fired inside
    unrelated words and pulled non-data jobs into the digest:

        "llm" matched Fu-llm-ent  -> retail fulfillment roles
        "etl" matched M-etl-ife   -> every MetLife posting
        "elt" matched D-elt-a     -> every Delta Air Lines posting

    Non-alphanumeric runs are treated as flexible separators so "power bi" and
    "power-bi" both match, and a trailing plural is allowed so "Data Scientists"
    and "AI Engineers" still match "data scientist" / "ai engineer" — strict
    boundaries alone silently dropped every pluralised title.
    """
    parts = [re.escape(tok) for tok in re.split(r"[^a-z0-9#+.]+", phrase.lower()) if tok]
    if not parts:
        return re.compile(r"(?!)")
    return re.compile(r"\b" + r"[^a-z0-9]+".join(parts) + r"(?:e?s)?\b")


def _has_keyword(text: str, phrase: str) -> bool:
    return _keyword_pattern(phrase).search(text) is not None


def _norm(title: str) -> str:
    t = (title or "").strip().lower()
    return re.sub(r"\s+", " ", t)


# ---------------------------------------------------------------------------
# Active track
#
# Every source module imports classify() directly, so the track is selected by
# a process-wide switch rather than threaded through 40 call sites. It defaults
# to "data", which keeps the existing pipeline byte-for-byte unchanged; only
# `--mode software` flips it, and that runs against its own database.
# ---------------------------------------------------------------------------

TRACK_DATA = "data"
TRACK_SOFTWARE = "software"

_active_track = TRACK_DATA


def set_active_track(track: str) -> None:
    """Select which keyword domain classify() scores against."""
    global _active_track
    if track not in (TRACK_DATA, TRACK_SOFTWARE):
        raise ValueError(f"Unknown track: {track!r}")
    _active_track = track


def get_active_track() -> str:
    return _active_track


def _seniority_cap(t: str) -> int:
    """Strictest seniority ceiling implied by a title.

    Every matching token is considered, not just the first one found. Stopping
    at the first match let token order decide the outcome: "Sr. Director" hit
    "senior" before "director" and was capped at 65 (surfaced as a match)
    instead of 34 (rejected), so director-level roles leaked through.
    """
    cap = 100
    for tok in SENIORITY_TOKENS:
        if re.search(rf"\b{re.escape(tok)}\b", t):
            cap = min(cap, 34 if tok in VERY_SENIOR else 65)
    return cap


def _classify_software(t: str) -> ClassifyResult:
    """Score a title for early-career software-engineering relevance."""
    # Data-domain roles belong to the data flow, not here.
    for phrase in DATA_DOMAIN_EXCLUDES:
        if phrase in t:
            return ClassifyResult(score=0, label="no")

    # Management and high-level IC titles are out of range for a 0-3 year search.
    for phrase in MANAGEMENT_EXCLUDES:
        if re.search(rf"\b{re.escape(phrase)}\b", t):
            return ClassifyResult(score=0, label="no")

    # Any seniority marker at all disqualifies a 0-3 year search. The shared cap
    # only demotes these to "maybe", which still surfaced real postings like
    # "Senior Software Engineer" and "Staff Software Engineer, Full Stack".
    # Senior/staff/lead are by definition outside the target range.
    if _seniority_cap(t) < 100:
        return ClassifyResult(score=0, label="no")

    # Job levels beyond II / 2 imply more than three years.
    if re.search(r"\b(?:iii|iv|v|vi)\b", t) or re.search(r"\b[3-9]\b", t):
        return ClassifyResult(score=0, label="no")

    # Non-software uses of "engineer"/"developer" (civil, sales, business dev).
    for phrase in SOFTWARE_HARD_EXCLUDES:
        if phrase in t:
            return ClassifyResult(score=0, label="no")

    strong = any(_has_keyword(t, p) for p in SOFTWARE_STRONG)
    weak = any(re.search(rf"\b{re.escape(p)}\b", t) for p in SOFTWARE_WEAK)
    if not (strong or weak):
        return ClassifyResult(score=0, label="no")

    score = 90 if strong else 55

    # Early-career signals are the point of this track, so they lift the score
    # rather than being neutral. Matched on word boundaries so "I" only hits
    # "Engineer I" and not any word containing an i.
    if any(re.search(rf"\b{re.escape(sig)}\b", t) for sig in EARLY_CAREER_SIGNALS):
        score = min(100, score + 8)

    # Seniority caps, same policy as the data track.
    score = min(score, _seniority_cap(t))

    score = max(0, min(score, 100))
    if score >= 70:
        label = "yes"
    elif score >= 40:
        label = "maybe"
    else:
        label = "no"
    return ClassifyResult(score=score, label=label)


def classify(title: str) -> ClassifyResult:
    """Score and label a job title for the active track's relevance."""
    t = _norm(title)
    if not t:
        return ClassifyResult(score=0, label="no")

    # ── ABSOLUTE FILTER: security clearance / citizenship ──────────────────
    # These are checked first and cannot be overridden by any safety-net.
    for phrase in CLEARANCE_EXCLUDE_PHRASES:
        if phrase in t:
            return ClassifyResult(score=0, label="no")
    for pat in CLEARANCE_EXCLUDE_REGEXES:
        if re.search(pat, t):
            return ClassifyResult(score=0, label="no")
    # ───────────────────────────────────────────────────────────────────────

    # Safety-net overrides that start with "data" but hit a hard-exclude phrase
    is_safety_net = any(override in t for override in DATA_SAFETY_NET_OVERRIDES)

    # Hard exclude regexes (internship etc.) — always reject, no override
    for pat in HARD_EXCLUDE_REGEXES:
        if re.search(pat, t):
            return ClassifyResult(score=0, label="no")

    # Software track diverges here, after the shared clearance and
    # internship/part-time filters have already been applied.
    if _active_track == TRACK_SOFTWARE:
        return _classify_software(t)

    # Hard exclude phrases — reject unless safety-net override
    if not is_safety_net:
        for phrase in HARD_EXCLUDES:
            if phrase in t:
                return ClassifyResult(score=0, label="no")

    # Base score from data-domain keyword match
    strong = any(_has_keyword(t, p) for p in DATA_STRONG)
    weak = any(_has_keyword(t, p) for p in DATA_WEAK)

    if not (strong or weak):
        return ClassifyResult(score=0, label="no")

    score = 90 if strong else 55

    # Seniority cap — senior/staff/principal → "maybe"; director/vp → "no"
    score = min(score, _seniority_cap(t))

    score = max(0, min(score, 100))

    if score >= 70:
        label = "yes"
    elif score >= 40:
        label = "maybe"
    else:
        label = "no"

    return ClassifyResult(score=score, label=label)


def is_match(title: str) -> bool:
    return classify(title).label in ("yes", "maybe")
