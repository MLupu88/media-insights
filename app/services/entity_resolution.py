"""Conservative canonical entity resolution for model-generated labels.

The grounded Chat contract must reject invented entities, but language models and
users may use an unambiguous shorthand for a real canonical value (for example
``Penny`` for ``Penny / Rewe``).  This module only supports exact,
case-insensitive, whitespace-normalized matches and exact delimited segments.
It deliberately does not perform fuzzy or substring matching.
"""

import re
from collections.abc import Iterable

_ENTITY_SEPARATOR_RE = re.compile(r"\s*(?:/|\||&|\+)\s*")
_ENTITY_WHITESPACE_RE = re.compile(r"\s+")


def normalize_entity_label(value: str) -> str:
    return _ENTITY_WHITESPACE_RE.sub(" ", value.strip().casefold())


def resolve_unique_entity_alias(value: str, candidates: Iterable[str]) -> str | None:
    """Return one canonical candidate when ``value`` resolves unambiguously.

    Resolution order:
    1. exact canonical value;
    2. case-insensitive / whitespace-normalized exact value;
    3. exact match to one segment separated by ``/``, ``|``, ``&`` or ``+``.

    Ambiguous and fuzzy matches return ``None`` and remain validation errors.
    """
    candidate_list = list(candidates)
    if value in candidate_list:
        return value

    normalized = normalize_entity_label(value)
    exact_matches = [
        candidate
        for candidate in candidate_list
        if normalize_entity_label(candidate) == normalized
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]

    segment_matches: list[str] = []
    for candidate in candidate_list:
        segments = _ENTITY_SEPARATOR_RE.split(candidate)
        if any(normalize_entity_label(segment) == normalized for segment in segments):
            segment_matches.append(candidate)

    return segment_matches[0] if len(segment_matches) == 1 else None
