"""Constants and shared copy for Phase 6C report exports (PowerPoint/Excel).

Mirrors the `*_contract.py` convention from Phase 6A/6B — a single, small
module of fixed values every report-building function imports, so the
same caps/thresholds are never redefined or drifted between the PPTX and
XLSX builders.
"""

# Display caps applied by each presentation format to the one, fully-
# retained ranked dataset the data layer fetches (see report_data.py) —
# never a second query at a different top_n.
REPORT_TOP_N = 10  # slide charts/tables
EXCEL_TOP_N = 50  # Excel ranked sheets — matches analytics.py's MAX_TOP_N

MAX_INSIGHTS_PER_REPORT = 12
MAX_ARTICLE_DETAIL_ROWS = 50_000

# PowerPoint-only presentation bounds — Excel never truncates a label or
# an insight's narrative text.
MAX_PPTX_LABEL_CHARS = 28
MAX_INSIGHT_TEXT_CHARS_PER_SLIDE = 600

# Failure bounds — exceeding these raises ReportTooLargeError rather than
# returning an oversized or corrupt-feeling response.
MAX_PPTX_BYTES = 20_000_000
MAX_XLSX_BYTES = 50_000_000

# Canonical population definition — same wording and voice already used in
# project_detail.html's own Analytics tab disclaimer, so the report and the
# web UI never describe the population differently. App-authored copy is
# English throughout this app (every phase so far); only model-generated
# insight `narrative` text (from Phase 6A, already Romanian) is not.
POPULATION_DEFINITION = (
    "Figures are scoped to unique valid articles (duplicates excluded), "
    "so coverage is never double-counted."
)

CHAT_EXCLUSION_NOTE = (
    "Chat answers (Phase 6B) are never included automatically in this "
    "report. Only validated interpretations (Phase 6A) appear below, "
    "explicitly labeled as such."
)

# Provider-neutral by design — never names the underlying model/vendor.
# Reinforces the existing causal-language filtering/validation/caveat
# behavior rather than replacing it.
AI_METHODOLOGY_NOTE = (
    "Insights are AI-assisted and grounded in the available project data. "
    "They are validated and never claim to prove causation."
)

NO_DATA_MESSAGE = "No data available for the selected filters."
