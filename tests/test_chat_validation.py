from app.schemas.chat import AnswerSubmission
from app.services.chat_validation import validate_answer

TOOL_RESULTS = [
    {
        "by_volume": [{"brand": "Auchan", "sov_pct": 24.6}, {"brand": "Carrefour", "sov_pct": 15.4}],
        "by_reach": [],
        "brand_count": 2,
        "requested_brand": {"brand": "Auchan", "sov_pct": 24.6},
        "available_filter_options": {
            "brands": ["Auchan", "Carrefour"],
            "publications": ["Ziarul Financiar"],
            "primary_topics": ["store_expansion"],
            "communication_categories": ["corporate"],
            "sentiments": ["positive"],
        },
    },
    {
        "articles": [
            {
                "article_id": "11111111-1111-1111-1111-111111111111",
                "title": "A",
                "article_url": "https://example.test/a",
                "mediatrust_url": None,
                "source": "Ziarul Financiar",
                "brand": "Auchan",
                "publication_date": None,
            }
        ],
        "count": 1,
    },
    {
        "insights": [
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "narrative_type": "brand_performance",
                "title": "Insight title",
                "narrative": "Insight text.",
                "related_brand": "Auchan",
                "related_topic": None,
                "related_publication": None,
                "related_story_key": None,
                "confidence": 0.9,
                "caveat": None,
            }
        ],
        "generation_id": "33333333-3333-3333-3333-333333333333",
    },
]

SNAPSHOT = {"tool_results": TOOL_RESULTS}


def _submission(**overrides) -> AnswerSubmission:
    base = dict(
        model="m",
        prompt_version="v1",
        payload_schema_version="p1",
        answer_text="Auchan a avut un SOV de 24,6%.",
        answer_type="fact",
        evidence=[
            {"kind": "metric", "tool_call_index": 0, "path": "requested_brand.sov_pct", "role": "value", "value": 24.6}
        ],
        related_brand=None,
        related_topic=None,
        related_publication=None,
        related_story_key=None,
        related_article_ids=[],
        source_urls=[],
    )
    base.update(overrides)
    return AnswerSubmission(**base)


def test_valid_answer_passes():
    result = validate_answer(_submission(), SNAPSHOT)
    assert result.valid, result.reason


def test_metric_tool_call_index_out_of_range():
    submission = _submission(
        evidence=[{"kind": "metric", "tool_call_index": 99, "path": "x", "role": "value", "value": 1}]
    )
    result = validate_answer(submission, SNAPSHOT)
    assert not result.valid
    assert "out of range" in result.reason


def test_metric_path_not_found():
    submission = _submission(
        evidence=[{"kind": "metric", "tool_call_index": 0, "path": "nope.nope", "role": "value", "value": 1}]
    )
    result = validate_answer(submission, SNAPSHOT)
    assert not result.valid
    assert "path not found" in result.reason


def test_metric_value_mismatch():
    submission = _submission(
        evidence=[
            {"kind": "metric", "tool_call_index": 0, "path": "requested_brand.sov_pct", "role": "value", "value": 99.9}
        ],
        answer_text="Text without matching numbers.",
    )
    result = validate_answer(submission, SNAPSHOT)
    assert not result.valid
    assert "value mismatch" in result.reason


def test_unknown_narrative_insight_id_rejected():
    submission = _submission(
        answer_text="Text.",
        evidence=[
            {"kind": "narrative_insight", "narrative_insight_id": "99999999-9999-9999-9999-999999999999"}
        ],
    )
    result = validate_answer(submission, SNAPSHOT)
    assert not result.valid
    assert "not returned by a get_valid_narrative_insights" in result.reason


def test_known_narrative_insight_id_accepted():
    submission = _submission(
        answer_text="Text.",
        answer_type="interpretation",
        evidence=[
            {"kind": "narrative_insight", "narrative_insight_id": "22222222-2222-2222-2222-222222222222"}
        ],
    )
    result = validate_answer(submission, SNAPSHOT)
    assert result.valid, result.reason


def test_unknown_related_brand_rejected():
    submission = _submission(related_brand="Lidl")
    result = validate_answer(submission, SNAPSHOT)
    assert not result.valid
    assert "Unknown brand" in result.reason


def test_known_related_brand_accepted():
    submission = _submission(related_brand="Auchan")
    result = validate_answer(submission, SNAPSHOT)
    assert result.valid, result.reason


def test_unknown_article_id_rejected():
    submission = _submission(related_article_ids=["44444444-4444-4444-4444-444444444444"])
    result = validate_answer(submission, SNAPSHOT)
    assert not result.valid
    assert "not in this run's article results" in result.reason


def test_known_article_id_accepted():
    submission = _submission(related_article_ids=["11111111-1111-1111-1111-111111111111"])
    result = validate_answer(submission, SNAPSHOT)
    assert result.valid, result.reason


def test_unknown_source_url_rejected():
    submission = _submission(source_urls=["https://not-in-pool.test/x"])
    result = validate_answer(submission, SNAPSHOT)
    assert not result.valid
    assert "Source URL" in result.reason


def test_known_source_url_accepted():
    submission = _submission(source_urls=["https://example.test/a"])
    result = validate_answer(submission, SNAPSHOT)
    assert result.valid, result.reason


# --- Numeric-claim-in-prose validation ----------------------------------------


def test_uncited_numeric_claim_in_prose_rejected():
    submission = _submission(answer_text="Auchan a avut un SOV de 24,6% si 999 de articole.")
    result = validate_answer(submission, SNAPSHOT)
    assert not result.valid
    assert "Uncited numeric claim" in result.reason


def test_romanian_decimal_comma_matches_evidence():
    submission = _submission(answer_text="SOV-ul Auchan a fost de 24,6%.")
    result = validate_answer(submission, SNAPSHOT)
    assert result.valid, result.reason


def test_cited_value_in_different_display_format_still_matches():
    snapshot = {
        "tool_results": [
            {
                "kpis": {"unique_valid_articles": 1234},
                "available_filter_options": {
                    "brands": [], "publications": [], "primary_topics": [],
                    "communication_categories": [], "sentiments": [],
                },
            }
        ]
    }
    submission = _submission(
        answer_text="Am inregistrat 1.234 de articole in aceasta perioada.",
        evidence=[
            {"kind": "metric", "tool_call_index": 0, "path": "kpis.unique_valid_articles", "role": "value", "value": 1234}
        ],
    )
    result = validate_answer(submission, snapshot)
    assert result.valid, result.reason


def test_year_ranking_and_quarter_numbers_allowlisted():
    snapshot = {
        "tool_results": [
            {
                "requested_brand": {"brand": "Auchan", "sov_pct": 24.6},
                "available_filter_options": {
                    "brands": ["Auchan"], "publications": [], "primary_topics": [],
                    "communication_categories": [], "sentiments": [],
                },
            }
        ]
    }
    submission = _submission(
        answer_text="In Q1 2026, Auchan a fost pe locul 1 cu un SOV de 24,6%.",
        evidence=[
            {"kind": "metric", "tool_call_index": 0, "path": "requested_brand.sov_pct", "role": "value", "value": 24.6}
        ],
    )
    result = validate_answer(submission, snapshot)
    assert result.valid, result.reason


def test_percentage_point_field_cited_as_percent_rejected():
    snapshot = {
        "tool_results": [
            {
                "brands": [{"brand": "Auchan", "sov_delta_pp": 5.4}],
                "available_filter_options": {
                    "brands": ["Auchan"], "publications": [], "primary_topics": [],
                    "communication_categories": [], "sentiments": [],
                },
            }
        ]
    }
    submission = _submission(
        answer_text="SOV-ul Auchan a crescut cu 5,4%.",
        evidence=[
            {"kind": "metric", "tool_call_index": 0, "path": "brands[0].sov_delta_pp", "role": "delta", "value": 5.4}
        ],
    )
    result = validate_answer(submission, snapshot)
    assert not result.valid
    assert "Percentage vs. percentage-point mismatch" in result.reason


def test_percentage_point_field_cited_correctly_accepted():
    snapshot = {
        "tool_results": [
            {
                "brands": [{"brand": "Auchan", "sov_delta_pp": 5.4}],
                "available_filter_options": {
                    "brands": ["Auchan"], "publications": [], "primary_topics": [],
                    "communication_categories": [], "sentiments": [],
                },
            }
        ]
    }
    submission = _submission(
        answer_text="SOV-ul Auchan a crescut cu 5,4pp.",
        evidence=[
            {"kind": "metric", "tool_call_index": 0, "path": "brands[0].sov_delta_pp", "role": "delta", "value": 5.4}
        ],
    )
    result = validate_answer(submission, snapshot)
    assert result.valid, result.reason


# --- Causal language -----------------------------------------------------------


def test_causal_language_rejected_without_insight_citation():
    submission = _submission(
        answer_text="SOV-ul a crescut din cauza campaniei recente, ajungand la 24,6%.",
    )
    result = validate_answer(submission, SNAPSHOT)
    assert not result.valid
    assert "Causal language" in result.reason


def test_causal_language_rejected_even_with_generic_insight_cited():
    """A cited NarrativeInsight is not proof of causation — the ban applies
    regardless of citation, per the explicit correction in the plan.
    """
    submission = _submission(
        answer_text="SOV-ul a crescut din cauza campaniei, ajungand la 24,6%.",
        answer_type="interpretation",
        evidence=[
            {"kind": "metric", "tool_call_index": 0, "path": "requested_brand.sov_pct", "role": "value", "value": 24.6},
            {"kind": "narrative_insight", "narrative_insight_id": "22222222-2222-2222-2222-222222222222"},
        ],
    )
    result = validate_answer(submission, SNAPSHOT)
    assert not result.valid
    assert "Causal language" in result.reason


def test_softer_association_language_permitted():
    submission = _submission(
        answer_text="Cresterea SOV la 24,6% este asociata cu campania recenta.",
    )
    result = validate_answer(submission, SNAPSHOT)
    assert result.valid, result.reason


# --- answer_type cross-check ---------------------------------------------------


def test_interpretation_without_insight_citation_rejected():
    submission = _submission(answer_type="interpretation")
    result = validate_answer(submission, SNAPSHOT)
    assert not result.valid
    assert "interpretation" in result.reason


def test_interpretation_with_insight_citation_accepted():
    submission = _submission(
        answer_text="Text.",
        answer_type="interpretation",
        evidence=[
            {"kind": "narrative_insight", "narrative_insight_id": "22222222-2222-2222-2222-222222222222"}
        ],
    )
    result = validate_answer(submission, SNAPSHOT)
    assert result.valid, result.reason


def test_recommendation_grounded_in_evidence_accepted():
    submission = _submission(answer_type="recommendation")
    result = validate_answer(submission, SNAPSHOT)
    assert result.valid, result.reason
