"""Deterministic grounding validator for a chat answer submission.

Checked, in order, against the run's persisted `answer_payload_snapshot`
(never freshly recomputed — see `app/models/chat.py`):

1. Structural — handled upstream by FastAPI/Pydantic parsing the request
   body into `AnswerSubmission` before this module ever runs (a chat answer
   is one submission, not a list of independently-salvageable candidates
   like 6A's insights, so there is nothing to "partially rescue" here).
2. Metric evidence — every `kind="metric"` entry's `tool_call_index` +
   `path` must resolve inside that tool's result in `tool_results`, and its
   `value` must match within the same float tolerance as 6A.
3. Narrative-insight evidence — every `kind="narrative_insight"` ID must
   have actually been returned by a `get_valid_narrative_insights` call
   present in `tool_results`.
4. Entity grounding — related brand/topic/publication/story must appear
   somewhere across `tool_results`.
5. Article grounding — related article IDs/URLs must appear in a
   `get_project_articles` result present in `tool_results`.
6. Numeric-claim-in-prose validation — every number/percentage mentioned in
   `answer_text` (not just formally cited evidence) must match a cited
   metric value, after normalizing Romanian/English formatting
   conventions, or be allowlisted non-metric context (dates, ranking
   positions, quarter/half labels). A matched percentage vs.
   percentage-point mismatch is also rejected.
7. Causal-language guard — a fixed marker list is banned outright,
   regardless of any narrative-insight citation (see
   `app/services/chat_contract.py`).
8. `answer_type` cross-check — `interpretation` requires a narrative-insight
   citation; `recommendation` still requires grounded evidence.

Nothing here ever recalculates a metric; it only confirms DeepSeek copied
numbers and identifiers that already exist in the bounded payload it was
given.
"""

import re
from dataclasses import dataclass

from app.schemas.chat import AnswerSubmission, ChatEvidenceReference
from app.services.chat_contract import CAUSAL_LANGUAGE_MARKERS, AnswerType
from app.services.entity_resolution import resolve_unique_entity_alias
from app.services.narrative_validation import (
    NUMERIC_TOLERANCE,
    PathResolutionError,
    numeric_match,
    resolve_path,
)

_NUMERIC_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*%?")
_YEAR_RE = re.compile(r"^20\d{2}$")
_RANK_PRECEDERS = ("top", "locul", "pozitia", "poziția")
_PP_MARKER_RE = re.compile(r"^\s*(pp\b|punct procentual|puncte procentuale)", re.IGNORECASE)


@dataclass
class ChatValidationResult:
    valid: bool
    reason: str | None = None
    canonical_related_brand: str | None = None


# --- Scanning tool_results for grounding pools ---------------------------------


def _scan_available_entities(tool_results: list[dict]) -> dict[str, set[str]]:
    brands: set[str] = set()
    topics: set[str] = set()
    publications: set[str] = set()
    story_keys: set[str] = set()

    def _absorb_options(options: dict | None) -> None:
        if not options:
            return
        brands.update(options.get("brands", []))
        topics.update(options.get("primary_topics", []))
        publications.update(options.get("publications", []))

    def _absorb_stories(section: dict | None) -> None:
        if not section:
            return
        for row in section.get("stories_by_volume", []) + section.get("stories_by_reach", []):
            story_keys.add(row["story_key"])

    for result in tool_results:
        _absorb_options(result.get("available_filter_options"))
        _absorb_stories(result.get("publications_and_stories"))
        _absorb_stories(result)  # get_story_clusters returns these at top level
        _absorb_stories(result.get("deltas"))  # get_period_comparison

        for period_key in ("baseline", "comparison"):
            period = result.get(period_key)
            if period:
                _absorb_options(period.get("available_filter_options"))
                _absorb_stories(period.get("publications_and_stories"))

    return {
        "brands": brands,
        "topics": topics,
        "publications": publications,
        "story_keys": story_keys,
    }


def _scan_article_pool(tool_results: list[dict]) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    urls: set[str] = set()
    for result in tool_results:
        for item in result.get("articles") or []:
            ids.add(item["article_id"])
            if item.get("article_url"):
                urls.add(item["article_url"])
            if item.get("mediatrust_url"):
                urls.add(item["mediatrust_url"])
    return ids, urls


def _scan_insight_ids(tool_results: list[dict]) -> set[str]:
    ids: set[str] = set()
    for result in tool_results:
        for item in result.get("insights") or []:
            ids.add(item["id"])
    return ids


# --- Numeric-claim extraction and normalization --------------------------------


def _normalize_numeric_token(token: str) -> float | None:
    raw = token.rstrip("%")
    if not raw:
        return None
    parts = re.split(r"[.,]", raw)
    if len(parts) == 1:
        return float(parts[0])
    last = parts[-1]
    if len(last) in (1, 2):
        # Last separator is decimal (matches this codebase's 1-decimal
        # rounding convention and the Romanian decimal comma); everything
        # before it is a thousands-grouped integer part, handling mixed
        # formats like "1.234,6" too.
        integer_part = "".join(parts[:-1])
        return float(f"{integer_part}.{last}")
    if len(last) == 3 and all(len(p) == 3 for p in parts[1:]):
        # Straight thousands grouping, either convention: "1.234" / "1,234"
        # / "12,345,678".
        return float("".join(parts))
    return None


def _is_allowlisted_context(answer_text: str, token: str, start: int) -> bool:
    bare = token.rstrip("%")
    if _YEAR_RE.fullmatch(bare):
        return True
    preceding = answer_text[max(0, start - 12) : start].strip().lower()
    last_word = preceding.rsplit(" ", 1)[-1].rstrip(".,:;") if preceding else ""
    if last_word in _RANK_PRECEDERS:
        return True
    if last_word in ("q", "h") and bare in ("1", "2", "3", "4"):
        return True
    if preceding.endswith(("q", "h")) and bare in ("1", "2", "3", "4"):
        return True
    return False


def _has_percent_marker(token: str, answer_text: str, end: int) -> bool:
    if token.endswith("%"):
        return True
    return answer_text[end : end + 3].lstrip().startswith("%")


def _has_pp_marker_after(answer_text: str, end: int) -> bool:
    return bool(_PP_MARKER_RE.match(answer_text[end : end + 40]))


def _infer_unit(path: str) -> str | None:
    last_segment = path.rsplit(".", 1)[-1].split("[")[0]
    if last_segment.endswith("_pp"):
        return "pp"
    if last_segment.endswith("_pct") or last_segment == "percentage_delta":
        return "percent"
    return None


def _validate_numeric_claims(answer_text: str, evidence: list[ChatEvidenceReference]) -> str | None:
    metric_entries = [e for e in evidence if e.kind == "metric"]

    for match in _NUMERIC_TOKEN_RE.finditer(answer_text):
        token = match.group()
        value = _normalize_numeric_token(token)
        if value is None:
            continue
        if _is_allowlisted_context(answer_text, token, match.start()):
            continue

        matching_entries = [
            entry for entry in metric_entries if abs(entry.value - value) < NUMERIC_TOLERANCE
        ]
        if not matching_entries:
            return f"Uncited numeric claim in answer text: {token!r}."

        unit_ok = False
        for entry in matching_entries:
            expected_unit = _infer_unit(entry.path)
            if expected_unit is None:
                unit_ok = True
                break
            if expected_unit == "percent" and _has_percent_marker(token, answer_text, match.end()):
                unit_ok = True
                break
            if expected_unit == "pp" and _has_pp_marker_after(answer_text, match.end()):
                unit_ok = True
                break
        if not unit_ok:
            return f"Percentage vs. percentage-point mismatch for {token!r}."

    return None


def _check_causal_language(answer_text: str) -> str | None:
    lowered = answer_text.lower()
    for marker in CAUSAL_LANGUAGE_MARKERS:
        if marker in lowered:
            return f"Causal language is not permitted in a chat answer: {marker!r}."
    return None


def _check_answer_type(submission: AnswerSubmission) -> str | None:
    has_insight_citation = any(e.kind == "narrative_insight" for e in submission.evidence)
    if submission.answer_type == AnswerType.INTERPRETATION and not has_insight_citation:
        return "answer_type='interpretation' requires at least one narrative_insight citation."
    if submission.answer_type == AnswerType.RECOMMENDATION and not submission.evidence:
        return "answer_type='recommendation' must still cite at least one evidence entry."
    return None


def validate_answer(
    submission: AnswerSubmission, answer_payload_snapshot: dict
) -> ChatValidationResult:
    tool_results: list[dict] = answer_payload_snapshot.get("tool_results", [])

    for index, entry in enumerate(submission.evidence):
        if entry.kind != "metric":
            continue
        if entry.tool_call_index < 0 or entry.tool_call_index >= len(tool_results):
            return ChatValidationResult(
                False,
                f"Evidence entry {index}: tool_call_index {entry.tool_call_index} out of range.",
            )
        try:
            actual = resolve_path(tool_results[entry.tool_call_index], entry.path)
        except PathResolutionError:
            return ChatValidationResult(
                False, f"Evidence entry {index}: path not found: {entry.path!r}."
            )
        if not numeric_match(actual, entry.value):
            return ChatValidationResult(
                False,
                f"Evidence entry {index}: value mismatch at {entry.path!r}: "
                f"tool result has {actual!r}, candidate cited {entry.value!r}.",
            )

    insight_pool = _scan_insight_ids(tool_results)
    for index, entry in enumerate(submission.evidence):
        if entry.kind != "narrative_insight":
            continue
        if str(entry.narrative_insight_id) not in insight_pool:
            return ChatValidationResult(
                False,
                f"Evidence entry {index}: narrative_insight_id "
                f"{entry.narrative_insight_id} was not returned by a "
                f"get_valid_narrative_insights call this run.",
            )

    available = _scan_available_entities(tool_results)
    canonical_related_brand = None
    if submission.related_brand:
        canonical_related_brand = resolve_unique_entity_alias(
            submission.related_brand, available["brands"]
        )
        if canonical_related_brand is None:
            return ChatValidationResult(False, f"Unknown brand: {submission.related_brand!r}.")
    if submission.related_topic and submission.related_topic not in available["topics"]:
        return ChatValidationResult(False, f"Unknown topic: {submission.related_topic!r}.")
    if (
        submission.related_publication
        and submission.related_publication not in available["publications"]
    ):
        return ChatValidationResult(
            False, f"Unknown publication: {submission.related_publication!r}."
        )
    if (
        submission.related_story_key
        and submission.related_story_key not in available["story_keys"]
    ):
        return ChatValidationResult(False, f"Unknown story: {submission.related_story_key!r}.")

    article_ids, article_urls = _scan_article_pool(tool_results)
    for article_id in submission.related_article_ids:
        if str(article_id) not in article_ids:
            return ChatValidationResult(
                False, f"Article {article_id} is not in this run's article results."
            )
    for url in submission.source_urls:
        if url not in article_urls:
            return ChatValidationResult(False, f"Source URL not recognized: {url!r}.")

    numeric_error = _validate_numeric_claims(submission.answer_text, submission.evidence)
    if numeric_error:
        return ChatValidationResult(False, numeric_error)

    causal_error = _check_causal_language(submission.answer_text)
    if causal_error:
        return ChatValidationResult(False, causal_error)

    answer_type_error = _check_answer_type(submission)
    if answer_type_error:
        return ChatValidationResult(False, answer_type_error)

    return ChatValidationResult(True, canonical_related_brand=canonical_related_brand)
