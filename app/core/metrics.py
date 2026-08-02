"""Prometheus metric definitions, shared by whatever instruments them
(RequestLoggingMiddleware, the chat/embeddings routes, GuardrailsService)
and by app/api/routes/metrics.py, which serves them all via
prometheus_client.generate_latest() at scrape time.

Counters/Histograms here are incremented live, at the moment something
happens -- prometheus_client tracks their state itself. The model
in-flight/queued and per-key usage Gauges are different: those mirror
state that already lives elsewhere (ModelConcurrencyLimiter,
UsageTracker), so app/api/routes/metrics.py sets their current value at
scrape time instead of incrementing them inline.
"""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS_TOTAL = Counter(
    "openbouncer_http_requests_total",
    "Total HTTP requests handled.",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "openbouncer_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "path"],
)

CHAT_COMPLETIONS_TOTAL = Counter(
    "openbouncer_chat_completions_total",
    "Total /v1/chat/completions requests.",
    ["model", "status"],
)
CHAT_COMPLETION_DURATION_SECONDS = Histogram(
    "openbouncer_chat_completion_duration_seconds",
    "/v1/chat/completions duration in seconds.",
    ["model", "guardrails"],
)

GUARDRAILS_REQUESTS_TOTAL = Counter(
    "openbouncer_guardrails_requests_total",
    "Requests processed by a guardrails config_id.",
    ["config_id"],
)
# No "blocked" label: NemoLibraryGuardrailsService.generate_async()'s plain
# return value (what we call it with) doesn't expose a reliable,
# config-agnostic "was this blocked" signal -- the actions that decide that
# (e.g. self_check_input) make the call deep inside nemoguardrails' own
# action dispatch, not surfaced to callers. String-matching refusal text
# would break for any operator-customized Colang flow. Follow-up if this is
# needed: switch to generate_async(options=GenerationOptions(...)), which
# can return richer log/output_data -- a bigger change to this class's
# calling convention than fits this round.

# app.guardrails.prompt_injection -- a separate, standalone pre-filter
# (independent of GUARDRAILS_MODE, see that module's docstring), unlike
# GUARDRAILS_REQUESTS_TOTAL above which is specifically about NeMo config_ids.
PROMPT_INJECTION_SCANNED_TOTAL = Counter(
    "openbouncer_prompt_injection_scanned_total",
    "Chat completion requests scanned by the prompt-injection guardrail (only counted while it's enabled).",
)
PROMPT_INJECTION_MATCHES_TOTAL = Counter(
    "openbouncer_prompt_injection_matches_total",
    "Prompt-injection matches found, by category and by which detection path found them.",
    ["category", "via"],
)
PROMPT_INJECTION_ACTIONS_TOTAL = Counter(
    "openbouncer_prompt_injection_actions_total",
    "Prompt-injection guardrail decisions, by the action actually applied to the request.",
    ["action"],
)
# category (9 fixed InjectionCategory values), via (5: direct/typoglycemia/
# base64/hex/char_spaced), action (3: flag/redact/block -- disabled/no-match
# requests aren't counted here) are all bounded, code-defined enums, never
# raw request content -- same cardinality discipline as the comment above.

# Set (not incremented) at scrape time from ModelRegistry's per-model
# ModelConcurrencyLimiter -- see app/api/routes/metrics.py.
MODEL_INFLIGHT_REQUESTS = Gauge(
    "openbouncer_model_inflight_requests",
    "Requests currently executing against a model's upstream, within its concurrency_limit.",
    ["model"],
)
MODEL_QUEUED_REQUESTS = Gauge(
    "openbouncer_model_queued_requests",
    "Requests waiting for a free concurrency slot for a model.",
    ["model"],
)

# Set (not incremented) at scrape time from UsageTracker -- see
# app/api/routes/metrics.py.
USAGE_REQUESTS_TOTAL = Gauge(
    "openbouncer_usage_requests_total",
    "Total requests recorded per API key (see app.auth.usage.UsageTracker).",
    ["key_id"],
)
USAGE_TOKENS_TOTAL = Gauge(
    "openbouncer_usage_tokens_total",
    "Total tokens recorded per API key, by kind (prompt/completion/total).",
    ["key_id", "kind"],
)
