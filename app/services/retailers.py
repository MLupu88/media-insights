import re

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


def infer_retailer(
    retailer_hint: str | None = None,
    filename: str | None = None,
    mapped_value: str | None = None,
) -> str:
    """Infer the canonical retailer using the documented priority order:
    explicit hint, then filename, then a mapped brand/retailer column, then unknown.
    """
    for candidate in (retailer_hint, filename, mapped_value):
        matched = match_retailer(candidate)
        if matched:
            return matched
    return UNKNOWN_RETAILER
