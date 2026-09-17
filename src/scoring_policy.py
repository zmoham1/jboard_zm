from __future__ import annotations

from dataclasses import dataclass
from statistics import median
import re

DEFAULT_YES_THRESHOLD = 70
DEFAULT_MAYBE_THRESHOLD = 40
MIN_THRESHOLD_FEEDBACK_ROWS = 8

# --- Structural walls -------------------------------------------------------
#
# Conditions that make a role un-appliable no matter how well the skills line
# up: the candidate is on F1 OPT, graduated, and does not hold a clearance.
# These were previously spread across weighted dimensions, where a wall cost a
# few points instead of ruling the role out. A SECRET-clearance posting scored
# 59 and arrived as "maybe" because the risk dimension carries 10% weight.
#
# A wall returns a reason string and forces the score to 0. Everything softer
# stays in the weighted dimensions where it belongs.

# Absolute: no context needed, the phrase alone settles it.
SPONSORSHIP_BLOCK_REGEXES = (
    r"\bno\s+(?:visa\s+)?sponsorship\b",
    r"\bwithout\s+(?:the\s+)?need\s+for\s+(?:current\s+or\s+future\s+)?(?:visa\s+)?sponsorship\b",
    r"\b(?:not|unable\s+to|cannot|will\s+not|do\s+not|does\s+not)\s+(?:be\s+able\s+to\s+)?(?:offer|provide|consider)\s+(?:visa\s+)?sponsorship\b",
    r"\bsponsorship\s+is\s+not\s+(?:available|offered|provided)\b",
    r"\bnot\s+(?:eligible|able)\s+to\s+sponsor\b",
    r"\bno\s+(?:stem\s+)?opt\b",
    r"\b(?:opt|stem\s+opt|cpt)\s+(?:candidates\s+)?(?:are\s+)?not\s+(?:eligible|accepted|considered)\b",
    r"\bmust\s+(?:be\s+)?(?:legally\s+)?authorized\s+to\s+work\s+.{0,40}\bwithout\s+sponsorship\b",
)

ENROLLMENT_BLOCK_REGEXES = (
    r"\bstudent\s+worker\b",
    r"\bcontract\s+student\b",
    r"\bmust\s+be\s+(?:currently\s+)?enrolled\b",
    r"\bcurrently\s+enrolled\s+in\s+(?:an?\s+)?(?:accredited\s+)?(?:degree|program|university|college)\b",
    r"\bactive\s+enrollment\s+(?:is\s+)?required\b",
)

# Physical or shift-based operations wearing a data-sounding title.
SHIFT_OPS_BLOCK_REGEXES = (
    r"\bshop\s+floor\b",
    r"\brotating\s+shifts?\b",
    r"\b24\s*/\s*7\s+(?:operation|coverage|environment|shift)",
    r"\bnight\s+shift\b",
    r"\bswing\s+shift\b",
    r"\bgraveyard\s+shift\b",
)

# Conditional: only a wall when the JD states them as required, because these
# tokens also appear in "nice to have" lists where they cost nothing.
PRODUCTION_LANGUAGE_TOKENS = (
    "c#", "c++", "java", "golang", "kotlin", "scala", ".net", "ruby on rails",
)

ENTERPRISE_PLATFORM_TOKENS = (
    "workday", "sap", "propel plm", "acumatica", "procore", "peoplesoft",
    "netsuite", "appian", "servicenow",
)

# Ownership language that marks a numbered/senior title as genuinely above the
# target band, rather than a title inflated by a company's levelling scheme.
SENIOR_OWNERSHIP_REGEXES = (
    r"\bmentor(?:ing|ship)?\s+(?:junior|other|the\s+team|engineers|scientists|analysts)\b",
    r"\b(?:own|drive|define|lead)\s+(?:the\s+)?(?:technical\s+)?(?:architecture|roadmap|strategy|vision)\b",
    r"\bset\s+(?:the\s+)?technical\s+direction\b",
    r"\btech(?:nical)?\s+lead\b",
)
# "II" is deliberately absent: Data Engineer II / BI Engineer I roles are
# squarely in the target band and have scored among the best fits. III and
# above, plus the word-titles, are the band where ownership language matters.
SENIOR_TITLE_REGEX = r"\b(?:senior|sr\.?|lead|principal|staff|iii|iv)\b"


def _search_any(patterns: tuple[str, ...], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0).strip()
    return ""

PREFERENCE_ACTIONS = frozenset({"interested", "applied", "dismissed", "archived", "shortlisted"})
OUTCOME_POSITIVE_ACTIONS = frozenset({"responded", "interview", "onsite", "offer"})
OUTCOME_NEGATIVE_ACTIONS = frozenset({"screen_reject", "rejected", "ghosted"})
STRONG_POSITIVE_ACTIONS = frozenset({"interview", "onsite", "offer"})


@dataclass(frozen=True)
class LabelThresholds:
    yes: int = DEFAULT_YES_THRESHOLD
    maybe: int = DEFAULT_MAYBE_THRESHOLD
    calibrated: bool = False
    positive_count: int = 0
    negative_count: int = 0
    reason: str = "default"


def structural_block_reason(
    title: str,
    description: str,
    *,
    required_text: str = "",
    clearance_reason: str = "",
) -> str:
    """Return why a role is un-appliable, or "" when no wall is present.

    ``required_text`` is the JD's requirements section when one could be
    parsed. Tokens like "java" or "workday" only count as walls inside it (or
    alongside explicit must-have wording), since the same words routinely
    appear under "nice to have", where they cost nothing.

    ``clearance_reason`` is passed in rather than recomputed: the caller already
    runs the clearance detection for its risk dimension, and that detection has
    a tuned phrase and regex set worth reusing.
    """
    title_text = (title or "").strip().lower()
    body = (description or "").lower()
    required = (required_text or "").lower()
    # Requirement-scoped text: the parsed requirements section when available,
    # otherwise nothing, so a bare mention never blocks on its own.
    scoped = required

    if clearance_reason:
        return clearance_reason

    hit = _search_any(SPONSORSHIP_BLOCK_REGEXES, body)
    if hit:
        return f"Blocked: the posting rules out visa sponsorship ({hit})."

    hit = _search_any(ENROLLMENT_BLOCK_REGEXES, body) or _search_any(ENROLLMENT_BLOCK_REGEXES, title_text)
    if hit:
        return f"Blocked: the role requires current student enrollment ({hit})."

    hit = _search_any(SHIFT_OPS_BLOCK_REGEXES, body)
    if hit:
        return f"Blocked: shift-based or on-site physical operations ({hit})."

    for token in PRODUCTION_LANGUAGE_TOKENS:
        # Word boundaries do not apply cleanly to c# / c++ / .net, so match
        # those literally and the alphabetic ones on a boundary. \bjava\b will
        # not fire on "javascript", and \bscala\b will not fire on "scalable".
        pattern = re.escape(token) if not token[0].isalpha() else rf"\b{re.escape(token)}\b"
        if scoped and re.search(pattern, scoped):
            return f"Blocked: {token} is a required core language, so this is a production software engineering role."

    for token in ENTERPRISE_PLATFORM_TOKENS:
        if scoped and re.search(rf"\b{re.escape(token)}\b", scoped):
            return f"Blocked: requires {token}, an enterprise platform not on the resume."

    # A senior title alone is a weighted penalty, not a wall. It becomes a wall
    # only when the responsibilities also describe owning architecture or
    # mentoring, which is the part that cannot be bridged by framing.
    if re.search(SENIOR_TITLE_REGEX, title_text):
        hit = _search_any(SENIOR_OWNERSHIP_REGEXES, body)
        if hit:
            return f"Blocked: senior title with ownership expectations ({hit})."

    return ""


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def label_for_score(score: int, *, yes_threshold: int = DEFAULT_YES_THRESHOLD, maybe_threshold: int = DEFAULT_MAYBE_THRESHOLD) -> str:
    if score >= yes_threshold:
        return "yes"
    if score >= maybe_threshold:
        return "maybe"
    return "no"


def calibrate_thresholds(feedback_rows: list[dict]) -> LabelThresholds:
    scored_rows = []
    for row in feedback_rows:
        action = (row.get("action") or "").strip().lower()
        try:
            score = int(row.get("score") or 0)
        except (TypeError, ValueError):
            continue
        if action not in OUTCOME_POSITIVE_ACTIONS | OUTCOME_NEGATIVE_ACTIONS:
            continue
        scored_rows.append((action, score))

    positives = [score for action, score in scored_rows if action in OUTCOME_POSITIVE_ACTIONS]
    negatives = [score for action, score in scored_rows if action in OUTCOME_NEGATIVE_ACTIONS]
    strong_positives = [score for action, score in scored_rows if action in STRONG_POSITIVE_ACTIONS]

    if len(scored_rows) < MIN_THRESHOLD_FEEDBACK_ROWS or len(positives) < 3 or len(negatives) < 3:
        return LabelThresholds(
            calibrated=False,
            positive_count=len(positives),
            negative_count=len(negatives),
            reason="insufficient_outcome_data",
        )

    positive_anchor = strong_positives or positives
    yes_threshold = round(median(positive_anchor) - 3)
    maybe_threshold = round((median(positives) + median(negatives)) / 2)
    maybe_threshold = _clamp(maybe_threshold, 38, 60)
    yes_threshold = _clamp(yes_threshold, max(68, maybe_threshold + 10), 85)

    return LabelThresholds(
        yes=yes_threshold,
        maybe=maybe_threshold,
        calibrated=True,
        positive_count=len(positives),
        negative_count=len(negatives),
        reason="outcome_calibrated",
    )


def resume_fit_cap(resume_match_score: int) -> tuple[int, str]:
    if resume_match_score >= 55:
        return 100, ""
    if resume_match_score >= 45:
        return 82, "Capped because the resume/JD match is only moderate."
    if resume_match_score >= 35:
        return 74, "Capped because the resume/JD match is weak for a high-confidence recommendation."
    if resume_match_score >= 25:
        return 64, "Capped because the JD overlap is thin and employer boosts should not rescue the role."
    return 54, "Capped because the resume/JD match is very weak."
