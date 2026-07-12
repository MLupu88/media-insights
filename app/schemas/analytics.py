import uuid

from pydantic import BaseModel


class AnalyticsFiltersOut(BaseModel):
    brand: str | None
    publication: str | None
    primary_topic: str | None
    communication_category: str | None
    sentiment: str | None
    state: str


class AvailableFilterOptions(BaseModel):
    brands: list[str]
    publications: list[str]
    primary_topics: list[str]
    communication_categories: list[str]
    sentiments: list[str]


class KpiSummary(BaseModel):
    total_imported_rows: int
    valid_rows: int
    invalid_rows: int
    duplicate_rows: int
    duplicate_share_pct: float
    cross_project_duplicates_excluded: int
    unique_valid_articles: int
    unique_classified_articles: int
    unique_unclassified_articles: int
    total_reach: float
    average_reach: float | None
    median_reach: float | None
    reach_missing_count: int
    publication_count: int
    low_confidence_count: int


class BrandRow(BaseModel):
    brand: str
    article_count: int
    sov_pct: float
    total_reach: float
    reach_share_pct: float
    average_reach: float | None
    median_reach: float | None
    primary_focus_count: int
    mentioned_only_count: int


class BrandPerformance(BaseModel):
    by_volume: list[BrandRow]
    by_reach: list[BrandRow]
    brand_count: int


class DistributionRow(BaseModel):
    value: str
    count: int
    pct: float
    total_reach: float


class TopicMixEntry(BaseModel):
    topic: str
    count: int
    pct: float


class TopicMixByBrand(BaseModel):
    brand: str
    topics: list[TopicMixEntry]


class TopicAnalytics(BaseModel):
    classified_count: int
    primary_topic_distribution: list[DistributionRow]
    secondary_topic_distribution: list[DistributionRow]
    classified_without_secondary_topic_count: int
    communication_category_distribution: list[DistributionRow]
    topic_mix_by_brand: list[TopicMixByBrand]
    top_topics_by_volume: list[DistributionRow]
    top_topics_by_reach: list[DistributionRow]


class SentimentByBrand(BaseModel):
    brand: str
    total: int
    counts: dict[str, int]


class BrandRoleDistributionRow(BaseModel):
    value: str
    count: int
    pct: float


class PrimaryFocusVsMentionedOnly(BaseModel):
    primary_focus: int
    mentioned_only: int
    primary_focus_pct: float
    mentioned_only_pct: float


class LowConfidenceItem(BaseModel):
    article_id: uuid.UUID
    title: str | None
    brand: str
    primary_topic: str
    confidence: float


class SentimentAnalytics(BaseModel):
    overall_distribution: list[DistributionRow]
    sentiment_by_brand: list[SentimentByBrand]
    brand_role_distribution: list[BrandRoleDistributionRow]
    primary_focus_vs_mentioned_only: PrimaryFocusVsMentionedOnly
    low_confidence_total_count: int
    low_confidence_items: list[LowConfidenceItem]


class PublicationRow(BaseModel):
    publication: str
    article_count: int
    volume_pct: float
    total_reach: float
    reach_pct: float


class StoryRow(BaseModel):
    story_key: str
    article_count: int
    total_reach: float


class ConcentrationMetrics(BaseModel):
    top3_volume_pct: float
    top5_volume_pct: float
    top3_reach_pct: float
    top5_reach_pct: float


class PublicationsAndStories(BaseModel):
    publications_by_volume: list[PublicationRow]
    publications_by_reach: list[PublicationRow]
    publication_concentration: ConcentrationMetrics
    stories_by_volume: list[StoryRow]
    stories_by_reach: list[StoryRow]
    story_concentration: ConcentrationMetrics
    classified_with_story_key_count: int
    classified_without_story_key_count: int
    unique_story_cluster_count: int


class ProjectAnalyticsResponse(BaseModel):
    project_id: uuid.UUID
    filters: AnalyticsFiltersOut
    available_filter_options: AvailableFilterOptions
    top_n: int
    kpis: KpiSummary
    brands: BrandPerformance
    topics: TopicAnalytics
    sentiment: SentimentAnalytics
    publications_and_stories: PublicationsAndStories
