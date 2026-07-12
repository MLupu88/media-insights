"""Deterministic grounding validator for candidate narrative insights.

Every candidate DeepSeek proposes is checked, in order, against the
generation's persisted `source_snapshot` (never freshly recomputed
analytics — see `app/services/narrative_payload.py`):

1. Structural (Pydantic) — malformed candidates are rejected individually,
   never failing the rest of the submission.
2. Evidence path resolution — every cited path must exist in the snapshot.
3. Exact numeric match — cited values must equal the snapshot's value within
   a tiny float-serialization tolerance, never a 1-decimal display rounding.
4. Entity grounding — cited brand/topic/publication/story must be known to
   the snapshot.
5. Article grounding — cited article IDs/URLs must be in the snapshot's
   bounded evidence pool.
6. Type/key uniqueness — no duplicate (narrative_type, key) among valid
   insights in one generation.
7. Scope validity — the narrative_type must be valid for the generation's
   scope (project vs. comparison).

Nothing here ever recalculates a metric; it only confirms DeepSeek copied
numbers and identifiers that already exist in the bounded payload it was
given.
"""

import re
from dataclasses import dataclass

from pydantic import ValidationError

from app.models.narrative import NarrativeGenerationStatus
from app.schemas.narrative import CandidateInsight
from app.services.narrative_contract import NarrativeTypes

NUMERIC_TOLERANCE = 1e-6

_PATH_SEGMENT_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


class PathResolutionError(Exception):
    pass


def resolve_path(data, path: str):
    """Safe nested lookup supporting `a.b[0].c` style dotted/indexed paths.
    Raises PathResolutionError if any segment is missing or out of range.
    """
    current = data
    matched_any = False
    for match in _PATH_SEGMENT_RE.finditer(path):
        matched_any = True
        key, index = match.groups()
        if key is not None:
            if not isinstance(current, dict) or key not in current:
                raise PathResolutionError(path)
            current = current[key]
        else:
            if not isinstance(current, list):
                raise PathResolutionError(path)
            idx = int(index)
            if idx < 0 or idx >= len(current):
                raise PathResolutionError(path)
            current = current[idx]
    if not matched_any:
        raise PathResolutionError(path)
    return current


def numeric_match(actual, expected) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
        return False
    return abs(float(actual) - float(expected)) < NUMERIC_TOLERANCE


def _available_entities(data: dict, scope: str) -> dict[str, set[str]]:
    options = data["available_filter_options"]
    brands = set(options["brands"])
    topics = set(options["primary_topics"])
    publications = set(options["publications"])

    if scope == "project":
        story_rows = (
            data["publications_and_stories"]["stories_by_volume"]
            + data["publications_and_stories"]["stories_by_reach"]
        )
    else:
        story_rows = data["deltas"]["stories_by_volume"] + data["deltas"]["stories_by_reach"]
    story_keys = {row["story_key"] for row in story_rows}

    return {
        "brands": brands,
        "topics": topics,
        "publications": publications,
        "story_keys": story_keys,
    }


@dataclass
class ValidationResult:
    valid: bool
    reason: str | None = None
    baseline_value: float | None = None
    comparison_value: float | None = None
    delta: float | None = None


def validate_candidate(
    raw: dict,
    snapshot: dict,
    scope: str,
    seen_keys: set[tuple[str, str]],
) -> tuple[ValidationResult, CandidateInsight | None]:
    try:
        candidate = CandidateInsight.model_validate(raw)
    except ValidationError as exc:
        first_error = exc.errors()[0] if exc.errors() else None
        message = first_error["msg"] if first_error else str(exc)
        return ValidationResult(False, f"Structural validation failed: {message}"), None

    valid_types = (
        NarrativeTypes.PROJECT_SCOPE_VALID if scope == "project" else NarrativeTypes.COMPARISON_SCOPE_VALID
    )
    if candidate.narrative_type not in valid_types:
        return (
            ValidationResult(
                False,
                f"narrative_type {candidate.narrative_type!r} is not valid for scope {scope!r}.",
            ),
            candidate,
        )

    dedup_key = (candidate.narrative_type, candidate.key)
    if dedup_key in seen_keys:
        return (
            ValidationResult(False, f"Duplicate (narrative_type, key) pair: {dedup_key!r}."),
            candidate,
        )

    data = snapshot["data"]
    summary_values: dict[str, float] = {}
    for entry in candidate.evidence:
        try:
            actual_value = resolve_path(data, entry.path)
        except PathResolutionError:
            return (
                ValidationResult(False, f"Evidence path not found: {entry.path!r}."),
                candidate,
            )
        if not numeric_match(actual_value, entry.value):
            return (
                ValidationResult(
                    False,
                    f"Evidence value mismatch at {entry.path!r}: "
                    f"snapshot has {actual_value!r}, candidate cited {entry.value!r}.",
                ),
                candidate,
            )
        if entry.role in ("baseline", "comparison", "delta"):
            summary_values[entry.role] = entry.value

    available = _available_entities(data, scope)

    if candidate.related_brand and candidate.related_brand not in available["brands"]:
        return (
            ValidationResult(False, f"Unknown brand: {candidate.related_brand!r}."),
            candidate,
        )
    if candidate.related_topic and candidate.related_topic not in available["topics"]:
        return (
            ValidationResult(False, f"Unknown topic: {candidate.related_topic!r}."),
            candidate,
        )
    if (
        candidate.related_publication
        and candidate.related_publication not in available["publications"]
    ):
        return (
            ValidationResult(
                False, f"Unknown publication: {candidate.related_publication!r}."
            ),
            candidate,
        )
    if (
        candidate.related_story_key
        and candidate.related_story_key not in available["story_keys"]
    ):
        return (
            ValidationResult(False, f"Unknown story: {candidate.related_story_key!r}."),
            candidate,
        )

    pool = snapshot["evidence_pool"]
    pool_ids = {item["article_id"] for item in pool}
    pool_urls = {item["article_url"] for item in pool if item["article_url"]} | {
        item["mediatrust_url"] for item in pool if item["mediatrust_url"]
    }

    for article_id in candidate.related_article_ids:
        if str(article_id) not in pool_ids:
            return (
                ValidationResult(
                    False, f"Article {article_id} is not in the bounded evidence pool."
                ),
                candidate,
            )
    for url in candidate.source_urls:
        if url not in pool_urls:
            return (
                ValidationResult(False, f"Source URL not recognized: {url!r}."),
                candidate,
            )

    return (
        ValidationResult(
            True,
            baseline_value=summary_values.get("baseline"),
            comparison_value=summary_values.get("comparison"),
            delta=summary_values.get("delta"),
        ),
        candidate,
    )


def compute_generation_outcome(
    requested_types: list[str], valid_narrative_types: set[str]
) -> tuple[str, list[str]]:
    """Derives the five-state status from which requested types ended up
    with at least one valid insight. See app/models/narrative.py for the
    status constants.
    """
    missing = [t for t in requested_types if t not in valid_narrative_types]
    if not missing:
        return NarrativeGenerationStatus.COMPLETE, []
    if len(missing) == len(requested_types):
        return NarrativeGenerationStatus.FAILED, missing
    return NarrativeGenerationStatus.PARTIALLY_COMPLETE, missing
