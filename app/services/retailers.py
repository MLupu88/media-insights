import re
from collections import Counter
from dataclasses import dataclass

from app.models.article import RetailerConfidence

UNKNOWN_RETAILER = "unknown"

CANONICAL_RETAILERS: tuple[str, ...] = (
    "Auchan",
    "Carrefour",
    "Kaufland",
    "Lidl",
    "Mega Image",
    "Metro",
    "Penny / Rewe",
    "Profi",
    "Selgros",
)

_RETAILER_ALIASES: dict[str, tuple[str, ...]] = {
    "Auchan": ("auchan",),
    "Carrefour": ("carrefour",),
    "Kaufland": ("kaufland",),
    "Lidl": ("lidl",),
    "Mega Image": ("mega image", "megaimage", "mega-image"),
    "Metro": ("metro",),
    "Penny / Rewe": ("penny rewe", "penny", "rewe"),
    "Profi": ("profi",),
    "Selgros": ("selgros",),
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Brand-detection safety thresholds (approved reporting-scope plan, §8/§5
# correction) — a bare percentage alone is unsafe at tiny sample sizes (a
# single 1/1 row would otherwise "win" at 100%), so both must hold before
# file-level dominance is trusted for a blank row.
MIN_DOMINANCE_SAMPLE_SIZE = 5
DOMINANCE_THRESHOLD_PCT = 90


def _normalize(text: str) -> str:
    return _NON_ALNUM.sub(" ", text.lower()).strip()


def match_retailer(text: str | None) -> str | None:
    if not text:
        return None
    normalized = f" {_normalize(text)} "
    for retailer in CANONICAL_RETAILERS:
        for alias in _RETAILER_ALIASES[retailer]:
            if f" {alias} " in normalized:
                return retailer
    return None


def compute_dominant_retailer(raw_values: list[str | None]) -> str | None:
    """Scans a sheet's per-row mapped brand-column values and returns the
    single canonical retailer they overwhelmingly agree on, or `None` if
    the sample is too small or the agreement too weak to trust.

    This is tier 3 of the brand-detection decision tree (`assign_retailer`
    below) — it is only ever consulted for a row whose *own* mapped value
    is blank; a present-but-unrecognized row value is never reached here.
    """
    recognized = [match_retailer(value) for value in raw_values if value]
    recognized = [value for value in recognized if value]
    if len(recognized) < MIN_DOMINANCE_SAMPLE_SIZE:
        return None
    counts = Counter(recognized)
    top_value, top_count = counts.most_common(1)[0]
    if (top_count / len(recognized)) * 100 >= DOMINANCE_THRESHOLD_PCT:
        return top_value
    return None


@dataclass
class RetailerAssignment:
    value: str
    confidence: str
    raw_value: str | None
    needs_review: bool


def assign_retailer(
    row_value: str | None,
    *,
    retailer_hint: str | None = None,
    retailer_hint_confirmed: bool = False,
    filename: str | None = None,
    dominant_value: str | None = None,
) -> RetailerAssignment:
    """Per-row brand-detection decision tree (approved reporting-scope
    plan, §8, with the safety correction from §5):

    1. A non-empty row value that resolves to a canonical retailer is
       assigned directly, at the highest confidence.
    2. A non-empty row value that does *not* resolve is preserved as-is
       and sent to review — full stop. It is never overridden by a
       confirmed hint, file-level dominance, or the filename; a
       present-but-unmatched value is never guessed past.
    3. A blank row value falls through, in order: a user-confirmed file
       hint; file-level dominance (only above both named thresholds);
       filename inference; otherwise review, with no raw value to show
       since no signal existed at all.
    """
    row_value = row_value.strip() if row_value else None

    if row_value:
        matched = match_retailer(row_value)
        if matched:
            return RetailerAssignment(matched, RetailerConfidence.EXPLICIT_COLUMN, row_value, False)
        return RetailerAssignment(UNKNOWN_RETAILER, RetailerConfidence.NEEDS_REVIEW, row_value, True)

    if retailer_hint_confirmed:
        matched = match_retailer(retailer_hint)
        if matched:
            return RetailerAssignment(matched, RetailerConfidence.CONFIRMED_MAPPING, None, False)

    if dominant_value:
        return RetailerAssignment(dominant_value, RetailerConfidence.FILE_LEVEL_INFERENCE, None, False)

    matched = match_retailer(filename)
    if matched:
        return RetailerAssignment(matched, RetailerConfidence.FILENAME_FALLBACK, None, False)

    return RetailerAssignment(UNKNOWN_RETAILER, RetailerConfidence.NEEDS_REVIEW, None, True)
