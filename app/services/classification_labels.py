"""Human-readable labels for the classification taxonomy's snake_case
values. Used only for display (Classification tab / Review tab templates)
-- every stored value, form field, and query filter always uses the raw
snake_case taxonomy value; only rendering ever goes through these maps.
"""

PRIMARY_TOPIC_LABELS: dict[str, str] = {
    "promotions_pricing": "Promotions & pricing",
    "products_private_label": "Products & private label",
    "store_expansion": "Store expansion",
    "financial_results": "Financial results",
    "investment_operations": "Investment & operations",
    "sustainability": "Sustainability",
    "csr_community": "CSR & community",
    "employer_branding": "Employer branding",
    "digital_ecommerce": "Digital & e-commerce",
    "logistics_operations": "Logistics & operations",
    "partnerships_campaigns": "Partnerships & campaigns",
    "market_research": "Market research",
    "leadership": "Leadership",
    "crisis_controversy": "Crisis & controversy",
    "regulation": "Regulation",
    "corporate_reputation": "Corporate reputation",
    "incidental_mention": "Incidental mention",
    "other": "Other",
}

COMMUNICATION_CATEGORY_LABELS: dict[str, str] = {
    "commercial": "Commercial",
    "corporate": "Corporate",
    "product": "Product",
    "employer_branding": "Employer branding",
    "csr": "CSR",
    "thought_leadership": "Thought leadership",
    "reactive_crisis": "Reactive crisis",
    "earned_editorial": "Earned editorial",
    "incidental": "Incidental",
}

SENTIMENT_LABELS: dict[str, str] = {
    "positive": "Positive",
    "neutral": "Neutral",
    "negative": "Negative",
    "mixed": "Mixed",
}

BRAND_ROLE_LABELS: dict[str, str] = {
    "primary_focus": "Primary focus",
    "secondary_mention": "Secondary mention",
    "incidental_mention": "Incidental mention",
}

REVIEW_STATUS_LABELS: dict[str, str] = {
    "pending": "Pending review",
    "approved": "Approved",
    "corrected": "Corrected",
}


def humanize_taxonomy_value(value: str | None, label_map: dict[str, str]) -> str:
    """Looks up a human label; falls back to a generic snake_case -> Title
    Case conversion for any value not in the map (defensive only -- every
    value accepted by the schema validators is always present above), and
    to an em dash for None.
    """
    if value is None:
        return "—"
    return label_map.get(value) or value.replace("_", " ").capitalize()
