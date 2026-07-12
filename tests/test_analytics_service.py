from app.models.article import ImportStatus
from app.services.analytics import (
    AnalyticsFilters,
    clamp_top_n,
    get_project_analytics,
    parse_analytics_filters,
)


# ---------------------------------------------------------------------------
# KPI math
# ---------------------------------------------------------------------------


def test_pipeline_kpis_read_directly_from_project_fields(db_session, project_factory):
    project = project_factory()
    project.total_rows = 10
    project.valid_rows = 8
    project.invalid_rows = 2
    project.duplicate_rows = 3
    db_session.commit()

    result = get_project_analytics(db_session, project, AnalyticsFilters())

    assert result["kpis"]["total_imported_rows"] == 10
    assert result["kpis"]["valid_rows"] == 8
    assert result["kpis"]["invalid_rows"] == 2
    assert result["kpis"]["duplicate_rows"] == 3
    assert result["kpis"]["duplicate_share_pct"] == 37.5  # 3/8 * 100


def test_unique_valid_articles_excludes_invalid_and_duplicate(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=2, import_status=ImportStatus.VALID, is_duplicate=False)
    article_factory(project, count=1, import_status=ImportStatus.VALID, is_duplicate=True)
    article_factory(project, count=1, import_status=ImportStatus.INVALID, is_duplicate=False)

    result = get_project_analytics(db_session, project, AnalyticsFilters())

    assert result["kpis"]["unique_valid_articles"] == 2


def test_publication_count_and_low_confidence_count(
    db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    a1, a2 = article_factory(project, count=1, source="Ziarul")[0], article_factory(
        project, count=1, source="Adevarul"
    )[0]
    classification_factory(a1, confidence=0.9)
    classification_factory(a2, confidence=0.3)

    result = get_project_analytics(db_session, project, AnalyticsFilters())

    assert result["kpis"]["publication_count"] == 2
    assert result["kpis"]["low_confidence_count"] == 1


# ---------------------------------------------------------------------------
# Reach: average, median, missing-value handling
# ---------------------------------------------------------------------------


def test_missing_reach_excluded_not_treated_as_zero(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, audience=1000.0)
    article_factory(project, count=1, audience=2000.0)
    article_factory(project, count=1, audience=None)

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    kpis = result["kpis"]

    assert kpis["total_reach"] == 3000.0
    # Average must be (1000+2000)/2 = 1500, NOT (1000+2000+0)/3 = 1000.
    assert kpis["average_reach"] == 1500.0
    assert kpis["median_reach"] == 1500.0
    assert kpis["reach_missing_count"] == 1


def test_average_and_median_reach_with_odd_count(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, audience=100.0)
    article_factory(project, count=1, audience=200.0)
    article_factory(project, count=1, audience=900.0)

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    kpis = result["kpis"]

    assert kpis["average_reach"] == 400.0
    assert kpis["median_reach"] == 200.0


def test_reach_stats_are_none_when_no_reach_recorded(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=2, audience=None)

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    kpis = result["kpis"]

    assert kpis["total_reach"] == 0.0
    assert kpis["average_reach"] is None
    assert kpis["median_reach"] is None
    assert kpis["reach_missing_count"] == 2


# ---------------------------------------------------------------------------
# Share of Voice / reach share
# ---------------------------------------------------------------------------


def test_sov_sums_to_100_percent_within_tolerance(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=3, retailer="Auchan")
    article_factory(project, count=2, retailer="Carrefour")
    article_factory(project, count=1, retailer="Lidl")

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    rows = result["brands"]["by_volume"]

    assert result["kpis"]["unique_valid_articles"] == 6
    total_sov = sum(row["sov_pct"] for row in rows)
    assert abs(total_sov - 100.0) <= 1.0


def test_reach_share_sums_to_100_percent_when_reach_present(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan", audience=1000.0)
    article_factory(project, count=1, retailer="Carrefour", audience=3000.0)
    article_factory(project, count=1, retailer="Lidl", audience=None)

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    rows = result["brands"]["by_volume"]

    total_reach_share = sum(row["reach_share_pct"] for row in rows)
    assert abs(total_reach_share - 100.0) <= 1.0


def test_one_article_contributes_to_exactly_one_brands_sov_numerator(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=4, retailer="Auchan")
    article_factory(project, count=3, retailer="Carrefour")

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    rows = result["brands"]["by_volume"]

    total_articles_across_brands = sum(row["article_count"] for row in rows)
    assert total_articles_across_brands == result["kpis"]["unique_valid_articles"] == 7


def test_sov_reflects_volume_reach_share_is_secondary(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=3, retailer="Auchan", audience=100.0)
    article_factory(project, count=1, retailer="Carrefour", audience=9000.0)

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    by_brand = {row["brand"]: row for row in result["brands"]["by_volume"]}

    # SOV is volume-based: Auchan has 3/4 articles = 75% SOV despite far lower reach.
    assert by_brand["Auchan"]["sov_pct"] == 75.0
    assert by_brand["Carrefour"]["sov_pct"] == 25.0
    # Reach share is the inverse here, confirming it's tracked independently.
    assert by_brand["Carrefour"]["reach_share_pct"] > by_brand["Auchan"]["reach_share_pct"]


# ---------------------------------------------------------------------------
# top_n is presentation-only
# ---------------------------------------------------------------------------


def test_top_n_does_not_alter_kpis_or_denominators(db_session, project_factory, article_factory):
    project = project_factory()
    for i in range(5):
        article_factory(project, count=1, retailer=f"Brand{i}")

    result_small = get_project_analytics(db_session, project, AnalyticsFilters(), top_n=1)
    result_large = get_project_analytics(db_session, project, AnalyticsFilters(), top_n=50)

    assert result_small["kpis"] == result_large["kpis"]
    assert result_small["brands"]["brand_count"] == result_large["brands"]["brand_count"] == 5
    # SOV percentages for the (single) returned row must be identical regardless
    # of top_n — they were computed against the full population either way.
    assert (
        result_small["brands"]["by_volume"][0]["sov_pct"]
        == result_large["brands"]["by_volume"][0]["sov_pct"]
    )


def test_top_n_only_truncates_returned_list_length(db_session, project_factory, article_factory):
    project = project_factory()
    for i in range(5):
        article_factory(project, count=1, retailer=f"Brand{i}")

    result = get_project_analytics(db_session, project, AnalyticsFilters(), top_n=2)

    assert len(result["brands"]["by_volume"]) == 2
    assert result["brands"]["brand_count"] == 5


def test_clamp_top_n_bounds():
    assert clamp_top_n("5") == 5
    assert clamp_top_n("999") == 50
    assert clamp_top_n("0") == 1
    assert clamp_top_n("not-a-number") == 10
    assert clamp_top_n(None) == 10


# ---------------------------------------------------------------------------
# Topic / category / sentiment distributions
# ---------------------------------------------------------------------------


def test_primary_topic_distribution(db_session, project_factory, article_factory, classification_factory):
    project = project_factory()
    a1 = article_factory(project, count=1)[0]
    a2 = article_factory(project, count=1)[0]
    a3 = article_factory(project, count=1)[0]
    classification_factory(a1, primary_topic="store_expansion")
    classification_factory(a2, primary_topic="store_expansion")
    classification_factory(a3, primary_topic="promotions_pricing")

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    dist = {row["value"]: row for row in result["topics"]["primary_topic_distribution"]}

    assert dist["store_expansion"]["count"] == 2
    assert dist["store_expansion"]["pct"] == round(2 / 3 * 100, 1)
    assert dist["promotions_pricing"]["count"] == 1


def test_secondary_topic_distribution_excludes_nulls_from_its_own_denominator(
    db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    a1 = article_factory(project, count=1)[0]
    a2 = article_factory(project, count=1)[0]
    classification_factory(a1, secondary_topic="investment_operations")
    classification_factory(a2, secondary_topic=None)

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    topics = result["topics"]

    assert topics["classified_without_secondary_topic_count"] == 1
    dist = topics["secondary_topic_distribution"]
    assert len(dist) == 1
    assert dist[0]["value"] == "investment_operations"
    assert dist[0]["pct"] == 100.0  # denominator is "has a secondary topic", not total classified


def test_communication_category_distribution(
    db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    a1 = article_factory(project, count=1)[0]
    a2 = article_factory(project, count=1)[0]
    classification_factory(a1, communication_category="corporate")
    classification_factory(a2, communication_category="commercial")

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    values = {row["value"] for row in result["topics"]["communication_category_distribution"]}

    assert values == {"corporate", "commercial"}


def test_topic_mix_by_brand(db_session, project_factory, article_factory, classification_factory):
    project = project_factory()
    a1 = article_factory(project, count=1, retailer="Auchan")[0]
    a2 = article_factory(project, count=1, retailer="Auchan")[0]
    a3 = article_factory(project, count=1, retailer="Carrefour")[0]
    classification_factory(a1, primary_topic="store_expansion")
    classification_factory(a2, primary_topic="store_expansion")
    classification_factory(a3, primary_topic="promotions_pricing")

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    mix = {row["brand"]: row["topics"] for row in result["topics"]["topic_mix_by_brand"]}

    assert mix["Auchan"] == [{"topic": "store_expansion", "count": 2, "pct": 100.0}]
    assert mix["Carrefour"] == [{"topic": "promotions_pricing", "count": 1, "pct": 100.0}]


def test_sentiment_overall_distribution_and_brand_role_split(
    db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    a1 = article_factory(project, count=1)[0]
    a2 = article_factory(project, count=1)[0]
    a3 = article_factory(project, count=1)[0]
    classification_factory(a1, sentiment="positive", brand_role="primary_focus")
    classification_factory(a2, sentiment="negative", brand_role="secondary_mention")
    classification_factory(a3, sentiment="positive", brand_role="incidental_mention")

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    sentiment = result["sentiment"]
    overall = {row["value"]: row["count"] for row in sentiment["overall_distribution"]}

    assert overall == {"positive": 2, "negative": 1}
    rollup = sentiment["primary_focus_vs_mentioned_only"]
    assert rollup["primary_focus"] == 1
    assert rollup["mentioned_only"] == 2  # secondary_mention + incidental_mention


def test_low_confidence_items_are_flagged_and_sorted(
    db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    a1 = article_factory(project, count=1, title="Low A")[0]
    a2 = article_factory(project, count=1, title="Low B")[0]
    a3 = article_factory(project, count=1, title="High")[0]
    classification_factory(a1, confidence=0.5)
    classification_factory(a2, confidence=0.2)
    classification_factory(a3, confidence=0.95)

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    sentiment = result["sentiment"]

    assert sentiment["low_confidence_total_count"] == 2
    titles_in_order = [item["title"] for item in sentiment["low_confidence_items"]]
    assert titles_in_order == ["Low B", "Low A"]  # lowest confidence first


# ---------------------------------------------------------------------------
# Publications and story clustering
# ---------------------------------------------------------------------------


def test_publication_rankings_and_concentration(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=3, source="Ziarul")
    article_factory(project, count=1, source="Adevarul")

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    pubs = result["publications_and_stories"]

    by_volume = {row["publication"]: row["article_count"] for row in pubs["publications_by_volume"]}
    assert by_volume == {"Ziarul": 3, "Adevarul": 1}
    # Top 3 by volume covers all 4 articles here (only 2 publications exist).
    assert pubs["publication_concentration"]["top3_volume_pct"] == 100.0


def test_story_clustering_is_classified_only_and_scoped_correctly(
    db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    a1 = article_factory(project, count=1)[0]
    a2 = article_factory(project, count=1)[0]
    a3 = article_factory(project, count=1)[0]  # unclassified
    classification_factory(a1, story_key="story-x")
    classification_factory(a2, story_key=None)
    # a3 stays unclassified entirely.

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    pubs = result["publications_and_stories"]

    assert pubs["classified_with_story_key_count"] == 1
    assert pubs["classified_without_story_key_count"] == 1
    assert pubs["unique_story_cluster_count"] == 1
    # Story concentration denominator is "classified with story_key" (=1), not
    # all 3 unique valid articles.
    assert pubs["story_concentration"]["top3_volume_pct"] == 100.0


def test_story_concentration_top3_vs_top5_never_mixed_with_reach(
    db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    for i in range(6):
        article = article_factory(project, count=1, audience=float(100 * (i + 1)))[0]
        classification_factory(article, story_key=f"story-{i}")

    result = get_project_analytics(db_session, project, AnalyticsFilters())
    concentration = result["publications_and_stories"]["story_concentration"]

    assert set(concentration.keys()) == {
        "top3_volume_pct",
        "top5_volume_pct",
        "top3_reach_pct",
        "top5_reach_pct",
    }
    # 6 equally-sized clusters: top 3 by volume = 50%, top 5 = 83.3%.
    assert concentration["top3_volume_pct"] == 50.0
    assert round(concentration["top5_volume_pct"], 1) == 83.3


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_brand_filter(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=2, retailer="Auchan")
    article_factory(project, count=3, retailer="Carrefour")

    result = get_project_analytics(db_session, project, AnalyticsFilters(brand="Auchan"))

    assert result["kpis"]["unique_valid_articles"] == 2
    assert len(result["brands"]["by_volume"]) == 1
    assert result["brands"]["by_volume"][0]["sov_pct"] == 100.0


def test_publication_filter(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=2, source="Ziarul")
    article_factory(project, count=1, source="Adevarul")

    result = get_project_analytics(db_session, project, AnalyticsFilters(publication="Adevarul"))

    assert result["kpis"]["unique_valid_articles"] == 1


def test_primary_topic_filter(db_session, project_factory, article_factory, classification_factory):
    project = project_factory()
    a1 = article_factory(project, count=1)[0]
    a2 = article_factory(project, count=1)[0]
    classification_factory(a1, primary_topic="store_expansion")
    classification_factory(a2, primary_topic="promotions_pricing")

    result = get_project_analytics(
        db_session, project, AnalyticsFilters(primary_topic="store_expansion")
    )

    assert result["kpis"]["unique_valid_articles"] == 1


def test_communication_category_filter(
    db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    a1 = article_factory(project, count=1)[0]
    a2 = article_factory(project, count=1)[0]
    classification_factory(a1, communication_category="corporate")
    classification_factory(a2, communication_category="commercial")

    result = get_project_analytics(
        db_session, project, AnalyticsFilters(communication_category="commercial")
    )

    assert result["kpis"]["unique_valid_articles"] == 1


def test_sentiment_filter(db_session, project_factory, article_factory, classification_factory):
    project = project_factory()
    a1 = article_factory(project, count=1)[0]
    a2 = article_factory(project, count=1)[0]
    classification_factory(a1, sentiment="positive")
    classification_factory(a2, sentiment="negative")

    result = get_project_analytics(db_session, project, AnalyticsFilters(sentiment="negative"))

    assert result["kpis"]["unique_valid_articles"] == 1


def test_state_filter_classified_and_unclassified(
    db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    a1 = article_factory(project, count=1)[0]
    article_factory(project, count=1)  # unclassified
    classification_factory(a1)

    classified_only = get_project_analytics(
        db_session, project, AnalyticsFilters(state="classified")
    )
    unclassified_only = get_project_analytics(
        db_session, project, AnalyticsFilters(state="unclassified")
    )

    assert classified_only["kpis"]["unique_valid_articles"] == 1
    assert unclassified_only["kpis"]["unique_valid_articles"] == 1


def test_available_filter_options_unaffected_by_other_active_filters(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan", source="Ziarul")
    article_factory(project, count=1, retailer="Carrefour", source="Adevarul")

    filtered_result = get_project_analytics(
        db_session, project, AnalyticsFilters(brand="Auchan")
    )

    # Even though we filtered to Auchan, the publication dropdown must still
    # list Adevarul (which only co-occurs with Carrefour).
    options = filtered_result["available_filter_options"]
    assert set(options["brands"]) == {"Auchan", "Carrefour"}
    assert set(options["publications"]) == {"Ziarul", "Adevarul"}


def test_raw_pipeline_kpis_unchanged_regardless_of_filters(
    db_session, project_factory, article_factory
):
    project = project_factory()
    project.total_rows = 20
    project.valid_rows = 15
    project.invalid_rows = 5
    project.duplicate_rows = 4
    db_session.commit()
    article_factory(project, count=2, retailer="Auchan")
    article_factory(project, count=1, retailer="Carrefour")

    unfiltered = get_project_analytics(db_session, project, AnalyticsFilters())
    filtered = get_project_analytics(db_session, project, AnalyticsFilters(brand="Auchan"))

    for key in ("total_imported_rows", "valid_rows", "invalid_rows", "duplicate_rows", "duplicate_share_pct"):
        assert unfiltered["kpis"][key] == filtered["kpis"][key]


def test_filters_apply_uniformly_brand_filter_degenerates_brand_section(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=2, retailer="Auchan")
    article_factory(project, count=5, retailer="Carrefour")

    result = get_project_analytics(db_session, project, AnalyticsFilters(brand="Auchan"))

    # Filtering to one brand and still looking at "brand performance" is
    # allowed — it just degenerates to a single 100%-SOV row.
    assert len(result["brands"]["by_volume"]) == 1
    assert result["brands"]["by_volume"][0]["sov_pct"] == 100.0


# ---------------------------------------------------------------------------
# Zero-data / partially-classified projects
# ---------------------------------------------------------------------------


def test_zero_data_project_returns_empty_structures_without_crashing(db_session, project_factory):
    project = project_factory()

    result = get_project_analytics(db_session, project, AnalyticsFilters())

    assert result["kpis"]["unique_valid_articles"] == 0
    assert result["kpis"]["average_reach"] is None
    assert result["brands"]["by_volume"] == []
    assert result["topics"]["primary_topic_distribution"] == []
    assert result["sentiment"]["overall_distribution"] == []
    assert result["publications_and_stories"]["publications_by_volume"] == []


def test_valid_but_entirely_unclassified_project(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=3, retailer="Auchan", audience=1000.0)

    result = get_project_analytics(db_session, project, AnalyticsFilters())

    # KPI/brand/publication sections work without any classification.
    assert result["kpis"]["unique_valid_articles"] == 3
    assert result["kpis"]["unique_classified_articles"] == 0
    assert result["kpis"]["unique_unclassified_articles"] == 3
    assert len(result["brands"]["by_volume"]) == 1
    # Topic/sentiment sections are empty, not broken.
    assert result["topics"]["primary_topic_distribution"] == []
    assert result["sentiment"]["overall_distribution"] == []


def test_partially_classified_project(
    db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    classified = article_factory(project, count=2, retailer="Auchan")
    article_factory(project, count=3, retailer="Auchan")  # stays unclassified
    for article in classified:
        classification_factory(article, primary_topic="store_expansion")

    result = get_project_analytics(db_session, project, AnalyticsFilters())

    assert result["kpis"]["unique_valid_articles"] == 5
    assert result["kpis"]["unique_classified_articles"] == 2
    assert result["kpis"]["unique_unclassified_articles"] == 3
    assert result["topics"]["classified_count"] == 2


# ---------------------------------------------------------------------------
# Filter parsing
# ---------------------------------------------------------------------------


def test_parse_analytics_filters_ignores_unknown_state():
    filters = parse_analytics_filters({"state": "bogus", "brand": " Auchan "})
    assert filters.state == "all"
    assert filters.brand == "Auchan"


def test_parse_analytics_filters_blank_values_become_none():
    filters = parse_analytics_filters({"brand": "", "publication": "   "})
    assert filters.brand is None
    assert filters.publication is None
