import uuid

from pydantic import BaseModel

from app.schemas.analytics import (
    AnalyticsFiltersOut,
    AvailableFilterOptions,
    BrandPerformance,
    KpiSummary,
    PublicationsAndStories,
    SentimentAnalytics,
    TopicAnalytics,
)


class PeriodAnalytics(BaseModel):
    project_ids: list[uuid.UUID]
    label: str
    project_count: int
    filters: AnalyticsFiltersOut
    available_filter_options: AvailableFilterOptions
    top_n: int
    kpis: KpiSummary
    brands: BrandPerformance
    topics: TopicAnalytics
    sentiment: SentimentAnalytics
    publications_and_stories: PublicationsAndStories


class KpiDelta(BaseModel):
    baseline: float | None
    comparison: float | None
    absolute_delta: float | None
    percentage_delta: float | None


class BrandDelta(BaseModel):
    brand: str
    baseline_sov_pct: float
    comparison_sov_pct: float
    sov_delta_pp: float
    baseline_reach_share_pct: float
    comparison_reach_share_pct: float
    reach_share_delta_pp: float
    baseline_rank: int | None
    comparison_rank: int | None
    rank_change: int | None
    is_new_entrant: bool
    is_dropout: bool


class DistributionDeltaItem(BaseModel):
    value: str
    baseline_count: int
    comparison_count: int
    baseline_pct: float
    comparison_pct: float
    pct_delta_pp: float
    is_new: bool
    is_gone: bool


class DistributionDeltas(BaseModel):
    rows: list[DistributionDeltaItem]
    emerging: list[DistributionDeltaItem]
    declining: list[DistributionDeltaItem]


class PublicationRankingDelta(BaseModel):
    publication: str
    baseline_rank: int | None
    comparison_rank: int | None
    rank_change: int | None
    is_new_entrant: bool
    is_dropout: bool


class StoryRankingDelta(BaseModel):
    story_key: str
    baseline_rank: int | None
    comparison_rank: int | None
    rank_change: int | None
    is_new_entrant: bool
    is_dropout: bool


class ConcentrationDeltaItem(BaseModel):
    baseline: float
    comparison: float
    delta_pp: float


class ConcentrationDeltas(BaseModel):
    top3_volume_pct: ConcentrationDeltaItem
    top5_volume_pct: ConcentrationDeltaItem
    top3_reach_pct: ConcentrationDeltaItem
    top5_reach_pct: ConcentrationDeltaItem


class ComparisonDeltas(BaseModel):
    kpis: dict[str, KpiDelta]
    brands: list[BrandDelta]
    topics: DistributionDeltas
    categories: DistributionDeltas
    sentiment: DistributionDeltas
    brand_role: DistributionDeltas
    publications_by_volume: list[PublicationRankingDelta]
    publications_by_reach: list[PublicationRankingDelta]
    stories_by_volume: list[StoryRankingDelta]
    stories_by_reach: list[StoryRankingDelta]
    publication_concentration: ConcentrationDeltas
    story_concentration: ConcentrationDeltas


class VolatilityMetrics(BaseModel):
    avg_rank_change: float | None
    entrants_count: int
    dropouts_count: int


class ComparisonVolatility(BaseModel):
    publications: VolatilityMetrics
    stories: VolatilityMetrics


class PeriodComparisonResponse(BaseModel):
    baseline: PeriodAnalytics
    comparison: PeriodAnalytics
    available_filter_options: AvailableFilterOptions
    top_n: int
    deltas: ComparisonDeltas
    volatility: ComparisonVolatility
