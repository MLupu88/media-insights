"""App-controlled contract identifiers and narrative-type taxonomy for
Phase 6A narrative generation.

`PROMPT_CONTRACT_VERSION`, `PAYLOAD_SCHEMA_VERSION`, and `VALIDATOR_VERSION`
are stamped onto every `NarrativeGeneration` at creation time from these
constants — never from n8n's results submission. n8n/DeepSeek's own
`model`/`prompt_version` metadata is stored separately, purely as an
informational record of what actually ran, and can never redefine which
contract, payload shape, or validation rules applied to a given generation.
"""

PROMPT_CONTRACT_VERSION = "narrative-v1"
PAYLOAD_SCHEMA_VERSION = "narrative-payload-v1"
VALIDATOR_VERSION = "narrative-validator-v1"


class NarrativeTypes:
    EXECUTIVE_SUMMARY = "executive_summary"
    COMPARISON_EXECUTIVE_SUMMARY = "comparison_executive_summary"
    KEY_FINDINGS = "key_findings"
    BRAND_PERFORMANCE = "brand_performance"
    SOV_REACH_INTERPRETATION = "sov_reach_interpretation"
    TOPIC_CATEGORY_SHIFTS = "topic_category_shifts"
    SENTIMENT_BRAND_ROLE = "sentiment_brand_role"
    PUBLICATION_STORY_MOVEMENT = "publication_story_movement"
    RISKS_OPPORTUNITIES = "risks_opportunities"
    RECOMMENDATIONS = "recommendations"
    METHODOLOGY_LIMITATIONS = "methodology_limitations"

    ALL: tuple[str, ...] = (
        EXECUTIVE_SUMMARY,
        COMPARISON_EXECUTIVE_SUMMARY,
        KEY_FINDINGS,
        BRAND_PERFORMANCE,
        SOV_REACH_INTERPRETATION,
        TOPIC_CATEGORY_SHIFTS,
        SENTIMENT_BRAND_ROLE,
        PUBLICATION_STORY_MOVEMENT,
        RISKS_OPPORTUNITIES,
        RECOMMENDATIONS,
        METHODOLOGY_LIMITATIONS,
    )

    # A caller may only request types valid for a generation's actual scope
    # (project vs. comparison); these are also the exact scope-specific
    # defaults applied when a caller omits `narrative_types`.
    PROJECT_SCOPE_VALID: tuple[str, ...] = (
        EXECUTIVE_SUMMARY,
        KEY_FINDINGS,
        BRAND_PERFORMANCE,
        SOV_REACH_INTERPRETATION,
        SENTIMENT_BRAND_ROLE,
        RISKS_OPPORTUNITIES,
        RECOMMENDATIONS,
        METHODOLOGY_LIMITATIONS,
    )

    COMPARISON_SCOPE_VALID: tuple[str, ...] = (
        COMPARISON_EXECUTIVE_SUMMARY,
        KEY_FINDINGS,
        BRAND_PERFORMANCE,
        SOV_REACH_INTERPRETATION,
        TOPIC_CATEGORY_SHIFTS,
        SENTIMENT_BRAND_ROLE,
        PUBLICATION_STORY_MOVEMENT,
        RISKS_OPPORTUNITIES,
        RECOMMENDATIONS,
        METHODOLOGY_LIMITATIONS,
    )

    PROJECT_SCOPE_DEFAULTS: tuple[str, ...] = PROJECT_SCOPE_VALID
    COMPARISON_SCOPE_DEFAULTS: tuple[str, ...] = COMPARISON_SCOPE_VALID
