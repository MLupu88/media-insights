"""App-controlled contract identifiers, bounds, and fixed vocabularies for
the Phase 6B chatbot.

Mirrors `app/services/narrative_contract.py`: these constants are stamped
onto every `ChatRun` at creation time and never taken from an n8n
submission.
"""

PROMPT_CONTRACT_VERSION = "chat-v1"
PAYLOAD_SCHEMA_VERSION = "chat-payload-v1"
VALIDATOR_VERSION = "chat-validator-v1"

MAX_TOOL_CALLS_PER_RUN = 4

# Bounds on user/model-controlled text and lists (see the plan's "Bounding
# user/model text" section) — rejected before persistence or any n8n round
# trip that would otherwise waste a call on an oversized payload.
MAX_QUESTION_LENGTH = 2_000
MAX_ANSWER_LENGTH = 8_000
MAX_CONVERSATION_HISTORY_MESSAGES = 6  # last 3 Q&A pairs
MAX_TOOL_PARAM_STRING_LENGTH = 200
MAX_EVIDENCE_ENTRIES = 20
MAX_RELATED_ARTICLE_IDS = 20
MAX_SOURCE_URLS = 20
MAX_TOOL_RESULTS_BYTES = 200_000


class AnswerType:
    FACT = "fact"
    INTERPRETATION = "interpretation"
    RECOMMENDATION = "recommendation"

    ALL: tuple[str, ...] = (FACT, INTERPRETATION, RECOMMENDATION)


class ChatRunStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"

    ALL = (PENDING, RUNNING, COMPLETE, FAILED)
    ACTIVE = (PENDING, RUNNING)
    TERMINAL = (COMPLETE, FAILED)


class ChatMessageRole:
    USER = "user"
    ASSISTANT = "assistant"

    ALL = (USER, ASSISTANT)


class ChatValidationStatus:
    VALID = "valid"
    REJECTED = "rejected"

    ALL = (VALID, REJECTED)


# Causal language is banned outright in 6B, even when a NarrativeInsight is
# cited — a valid 6A insight is not proof of causation, and NarrativeInsight
# has no causal-support field to gate on. Softer movement/association/
# contribution language is deliberately NOT on this list.
CAUSAL_LANGUAGE_MARKERS: tuple[str, ...] = (
    "a cauzat",
    "a cauzata",
    "cauzeaza",
    "cauzează",
    "din cauza",
    "din pricina",
    "a determinat",
    "a dus la",
    "responsabil pentru",
    "responsabila pentru",
    "din vina",
    "because",
    "due to",
    "caused by",
    "resulted in",
    "led to",
    "is responsible for",
    "was responsible for",
)

# Explicitly NOT banned (softer, permitted language): "a contribuit la",
# "asociat cu", "a influențat", "contributed to", "associated with",
# "likely driven by", "correlat cu".
