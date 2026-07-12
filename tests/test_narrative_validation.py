from app.services.narrative_validation import (
    PathResolutionError,
    compute_generation_outcome,
    resolve_path,
    validate_candidate,
)

PROJECT_SNAPSHOT = {
    "scope": "project",
    "data": {
        "kpis": {"unique_valid_articles": 42, "average_reach": 1234.5},
        "brands": {
            "by_volume": [
                {"brand": "Auchan", "sov_pct": 60.0},
                {"brand": "Carrefour", "sov_pct": 40.0},
            ]
        },
        "available_filter_options": {
            "brands": ["Auchan", "Carrefour"],
            "publications": ["Ziarul Financiar"],
            "primary_topics": ["store_expansion"],
            "communication_categories": ["corporate"],
            "sentiments": ["positive"],
        },
        "publications_and_stories": {
            "stories_by_volume": [{"story_key": "story-1", "article_count": 5}],
            "stories_by_reach": [{"story_key": "story-1", "article_count": 5}],
        },
    },
    "evidence_pool": [
        {
            "article_id": "11111111-1111-1111-1111-111111111111",
            "article_url": "https://example.test/a",
            "mediatrust_url": None,
            "brand": "Auchan",
            "title": "A",
            "source": "Ziarul Financiar",
            "publication_date": None,
        }
    ],
}


def _candidate(**overrides) -> dict:
    base = {
        "narrative_type": "executive_summary",
        "key": "main",
        "title": "Title",
        "narrative": "Narrative text.",
        "evidence_type": "kpi_delta",
        "evidence": [{"path": "kpis.unique_valid_articles", "role": "value", "value": 42}],
        "related_brand": None,
        "related_topic": None,
        "related_publication": None,
        "related_story_key": None,
        "related_article_ids": [],
        "source_urls": [],
        "confidence": 0.9,
        "caveat": None,
    }
    base.update(overrides)
    return base


def test_resolve_path_dict_and_index():
    assert resolve_path(PROJECT_SNAPSHOT["data"], "kpis.unique_valid_articles") == 42
    assert resolve_path(PROJECT_SNAPSHOT["data"], "brands.by_volume[0].sov_pct") == 60.0


def test_resolve_path_missing_key_raises():
    try:
        resolve_path(PROJECT_SNAPSHOT["data"], "kpis.does_not_exist")
        assert False, "expected PathResolutionError"
    except PathResolutionError:
        pass


def test_resolve_path_index_out_of_range_raises():
    try:
        resolve_path(PROJECT_SNAPSHOT["data"], "brands.by_volume[5].sov_pct")
        assert False, "expected PathResolutionError"
    except PathResolutionError:
        pass


def test_valid_candidate_passes():
    result, candidate = validate_candidate(_candidate(), PROJECT_SNAPSHOT, "project", set())
    assert result.valid, result.reason
    assert candidate is not None


def test_structural_failure_returns_no_candidate():
    raw = _candidate()
    del raw["title"]
    result, candidate = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert not result.valid
    assert candidate is None
    assert "Structural validation failed" in result.reason


def test_unresolvable_evidence_path_rejected():
    raw = _candidate(evidence=[{"path": "kpis.nope", "role": "value", "value": 1}])
    result, _candidate_obj = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert not result.valid
    assert "not found" in result.reason


def test_numeric_mismatch_rejected():
    raw = _candidate(evidence=[{"path": "kpis.unique_valid_articles", "role": "value", "value": 41}])
    result, _c = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert not result.valid
    assert "mismatch" in result.reason


def test_numeric_mismatch_hidden_by_one_decimal_rounding_still_rejected():
    """A value that would pass a naive round(x, 1) comparison must still be
    rejected under exact/tolerance validation — proves precision item 4.
    """
    snapshot = {
        **PROJECT_SNAPSHOT,
        "data": {**PROJECT_SNAPSHOT["data"], "kpis": {"unique_valid_articles": 24.649999, "average_reach": 1.0}},
    }
    raw = _candidate(evidence=[{"path": "kpis.unique_valid_articles", "role": "value", "value": 24.6}])
    result, _c = validate_candidate(raw, snapshot, "project", set())
    assert not result.valid
    assert "mismatch" in result.reason


def test_numeric_match_within_float_tolerance_accepted():
    snapshot = {
        **PROJECT_SNAPSHOT,
        "data": {**PROJECT_SNAPSHOT["data"], "kpis": {"unique_valid_articles": 24.6000001, "average_reach": 1.0}},
    }
    raw = _candidate(evidence=[{"path": "kpis.unique_valid_articles", "role": "value", "value": 24.6}])
    result, _c = validate_candidate(raw, snapshot, "project", set())
    assert result.valid, result.reason


def test_multiple_evidence_entries_validated_independently():
    raw = _candidate(
        evidence=[
            {"path": "brands.by_volume[0].sov_pct", "role": "baseline", "value": 60.0},
            {"path": "brands.by_volume[1].sov_pct", "role": "comparison", "value": 40.0},
            {"path": "kpis.unique_valid_articles", "role": "delta", "value": 999},
        ]
    )
    result, _c = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert not result.valid
    assert "kpis.unique_valid_articles" in result.reason


def test_multiple_evidence_entries_all_valid_populate_summary_values():
    raw = _candidate(
        evidence=[
            {"path": "brands.by_volume[0].sov_pct", "role": "baseline", "value": 60.0},
            {"path": "brands.by_volume[1].sov_pct", "role": "comparison", "value": 40.0},
        ]
    )
    result, _c = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert result.valid
    assert result.baseline_value == 60.0
    assert result.comparison_value == 40.0


def test_unknown_brand_rejected():
    raw = _candidate(related_brand="Lidl")
    result, _c = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert not result.valid
    assert "Unknown brand" in result.reason


def test_unknown_topic_rejected():
    raw = _candidate(related_topic="not_a_topic")
    result, _c = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert not result.valid
    assert "Unknown topic" in result.reason


def test_unknown_publication_rejected():
    raw = _candidate(related_publication="Made Up Gazette")
    result, _c = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert not result.valid
    assert "Unknown publication" in result.reason


def test_unknown_story_key_rejected():
    raw = _candidate(related_story_key="not-a-story")
    result, _c = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert not result.valid
    assert "Unknown story" in result.reason


def test_known_entities_accepted():
    raw = _candidate(
        related_brand="Auchan",
        related_topic="store_expansion",
        related_publication="Ziarul Financiar",
        related_story_key="story-1",
    )
    result, _c = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert result.valid, result.reason


def test_unknown_article_id_rejected():
    raw = _candidate(related_article_ids=["22222222-2222-2222-2222-222222222222"])
    result, _c = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert not result.valid
    assert "evidence pool" in result.reason


def test_known_article_id_accepted():
    raw = _candidate(related_article_ids=["11111111-1111-1111-1111-111111111111"])
    result, _c = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert result.valid, result.reason


def test_unknown_source_url_rejected():
    raw = _candidate(source_urls=["https://not-in-pool.test/x"])
    result, _c = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert not result.valid
    assert "Source URL" in result.reason


def test_known_source_url_accepted():
    raw = _candidate(source_urls=["https://example.test/a"])
    result, _c = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert result.valid, result.reason


def test_duplicate_key_rejected():
    seen = {("executive_summary", "main")}
    result, _c = validate_candidate(_candidate(), PROJECT_SNAPSHOT, "project", seen)
    assert not result.valid
    assert "Duplicate" in result.reason


def test_scope_mismatch_rejected_for_project_scope():
    raw = _candidate(narrative_type="comparison_executive_summary", key="cmp")
    result, _c = validate_candidate(raw, PROJECT_SNAPSHOT, "project", set())
    assert not result.valid
    assert "not valid for scope" in result.reason


def test_scope_mismatch_rejected_for_comparison_scope():
    comparison_snapshot = {
        "scope": "comparison",
        "data": {
            **PROJECT_SNAPSHOT["data"],
            "deltas": {
                "stories_by_volume": [{"story_key": "story-1"}],
                "stories_by_reach": [{"story_key": "story-1"}],
            },
        },
        "evidence_pool": PROJECT_SNAPSHOT["evidence_pool"],
    }
    raw = _candidate(narrative_type="executive_summary")
    result, _c = validate_candidate(raw, comparison_snapshot, "comparison", set())
    assert not result.valid
    assert "not valid for scope" in result.reason


def test_compute_generation_outcome_complete():
    status, missing = compute_generation_outcome(["executive_summary", "key_findings"], {"executive_summary", "key_findings"})
    assert status == "complete"
    assert missing == []


def test_compute_generation_outcome_partially_complete():
    status, missing = compute_generation_outcome(["executive_summary", "key_findings"], {"executive_summary"})
    assert status == "partially_complete"
    assert missing == ["key_findings"]


def test_compute_generation_outcome_failed():
    status, missing = compute_generation_outcome(["executive_summary", "key_findings"], set())
    assert status == "failed"
    assert missing == ["executive_summary", "key_findings"]
