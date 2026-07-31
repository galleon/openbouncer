# OpenBouncer

[![Tests](https://github.com/galleon/openbouncer/actions/workflows/tests.yml/badge.svg)](https://github.com/galleon/openbouncer/actions/workflows/tests.yml)

An OpenAI-compatible LLM gateway with pluggable guardrails.

See [SOVEREIGN.md](SOVEREIGN.md) for the project's positioning: a small,
fully-inspectable policy layer for self-hosted LLMs, with zero-network
guardrails and sovereignty-tagged model routing as the two properties
that differentiate it from other self-hostable gateways.

## Quick start

```bash
uv sync --extra dev
uv run uvicorn app.main:app --reload
uv run pytest
```

Endpoints: `GET /healthz` (no auth), `GET /v1/models`, `POST /v1/chat/completions`
(supports `stream: true`), `POST /v1/embeddings`. All `/v1/*` endpoints
require a bearer API key -- see [Authentication](#authentication) below.

`uv run pytest` above is the fast suite (no browser, no network beyond
Redis/upstream mocks) -- excluded from it (via `pyproject.toml`'s
`addopts`) is a separate browser-based smoke suite (`tests/e2e/`) that
drives the three [Web UI](#web-ui) pages in a real headless browser via
[Playwright](https://playwright.dev). Both run in CI on every push (see
`.github/workflows/tests.yml`'s `test` and `e2e` jobs) but are kept as
separate local commands, since the e2e suite needs a downloaded browser
binary, not just a Python package, and would otherwise slow down every
`uv run pytest` during normal development:

```bash
uv sync --extra e2e
uv run playwright install chromium
uv run pytest -m e2e
```

## Web UI

A small browser UI is served at `/ui` -- plain HTML/CSS/vanilla JS, no build
step (`app/ui/static/`). It's a manual test/demo client, not a replacement
for the API: paste an API key (kept in `localStorage`, sent as a normal
`Authorization: Bearer` header) and it lets you pick a model, optionally
turn on guardrails (config + preset), type a message with an optional image
URL, and send it with or without `stream: true` -- streamed responses render
live via `fetch()` + `ReadableStream`, parsing the same SSE frames documented
under [Streaming chat completion](#streaming-chat-completion).

It's backed by two read-only, auth-required endpoints that exist only for
the UI (not part of the OpenAI-compatible surface):

- `GET /api/ui/models` -- like `/v1/models`, filtered to the key's
  `allowed_models`, but each entry also includes `capabilities` so the UI
  can label models (and could filter by them, e.g. to hide non-chat models).
- `GET /api/ui/guardrails/configs` -- `{"configs": [...], "presets": [...]}`.
  `configs` comes from `GuardrailsCatalogService`
  (`app/guardrails/catalog.py`), which lists subdirectories of
  `GUARDRAILS_NEMO_LIBRARY_CONFIG_PATH` that look like valid `nemo_library`
  configs (a `config.yml`/`config.yaml` present, a valid-looking
  `config_id`) -- cheaply, without parsing them, so it never risks leaking a
  config's prompts/internals and stays fast enough to call on every page
  load. `presets` is a small fixed set of example scenarios (e.g. "jailbreak
  attempt") meant to auto-fill the message box and demonstrate a guardrails
  config's behavior; selecting one doesn't currently change which rails run
  server-side (see below).

When the UI's guardrails toggle is on, it sends:

```json
"guardrails": { "enabled": true, "config_id": "<selected config>", "preset": "<selected preset>" }
```

and omits the `guardrails` key entirely when off. `enabled`/`preset` are new
fields on the existing `guardrails` request object (`config_id` already
existed) -- `enabled: false` (or omitting the object) makes
`/v1/chat/completions` skip `GuardrailsService` entirely for that request,
regardless of `GUARDRAILS_MODE`. `preset` is accepted and threaded through
end to end (including into the request logs) but, as of now, doesn't alter
rail selection -- it exists for UI convenience and reproducibility, not as a
new guardrails behavior.

Note this is a per-*request* toggle: it has no effect unless the gateway's
own `GUARDRAILS_MODE` is actually set to `nemo_library` or
`nemo_microservice` (see [Guardrails](#guardrails)) -- with the default
`disabled` mode, a request with `guardrails.enabled: true` is accepted but
silently passes straight through, same as if it had been omitted.

## Running with Docker

```bash
cp config/api_keys.example.yaml config/api_keys.yaml   # then edit in a real key hash, see Authentication below
export UPSTREAM_NVIDIA_API_KEY=nvapi-...                # needed for chat/embeddings to actually reach NVIDIA
docker compose up --build
```

The gateway listens on `http://localhost:8000` (override with `GATEWAY_PORT`).
`./config` and `./guardrails_configs` are bind-mounted read-only into the
container, so editing the model registry, API keys, or guardrails configs on
the host takes effect on container restart without rebuilding the image.
(The [Admin API](#admin-api) needs these writable instead -- opt in with
`docker-compose.admin.yml` rather than editing this default.)

Config is passed via environment variables (see `docker-compose.yml`):

| Variable | Purpose |
| --- | --- |
| `MODEL_CONFIG_PATH` | Path to an alternate model registry YAML (alias of `OPENBOUNCER_MODELS_CONFIG`; unset uses the bundled `config/models.yaml`). |
| `GUARDRAILS_MODE` | `disabled` (default) / `nemo_library` / `nemo_microservice`. |
| `NEMO_GUARDRAILS_BASE_URL` | Base URL of a NeMo Guardrails Microservice, when `GUARDRAILS_MODE=nemo_microservice` (alias of `GUARDRAILS_NEMO_BASE_URL`). |
| `UPSTREAM_NVIDIA_API_KEY` | Upstream API key for the bundled NVIDIA-hosted models (matches `config/models.yaml`'s `api_key_env`). |
| `LOG_LEVEL` | Root logging level, e.g. `DEBUG` / `INFO` / `WARNING` (default `INFO`). |

Optional Redis-backed (instead of in-memory) rate limiting *and* usage
accounting, for running multiple gateway replicas against one shared
budget and one shared set of per-key usage totals:

```bash
echo "REDIS_URL=redis://redis:6379/0" >> .env
docker compose --profile redis up --build
```

Without the `redis` profile and `REDIS_URL`, the gateway just uses its
built-in in-memory limiter and tracker -- no code or config changes needed
either way (both `app/auth/rate_limiter.py` and `app/auth/usage.py` switch
on the same `REDIS_URL`).

### Supply chain

Every push to `main` that passes the [test suite](#quick-start) triggers
`.github/workflows/docker-publish.yml`, which builds the image in this
repo's `Dockerfile` and publishes `ghcr.io/galleon/openbouncer` with:

- **A [CycloneDX](https://cyclonedx.org/) SBOM** (via
  [`anchore/sbom-action`](https://github.com/anchore/sbom-action)) --
  every OS package (from the `python:3.12-slim` base) and every Python
  dependency actually shipped in the image, not just what's declared in
  `pyproject.toml`. Downloadable from the workflow run's artifacts, and
  attached to the image itself as a signed attestation.
- **A keyless [cosign](https://docs.sigstore.dev/cosign/overview/)
  signature** -- tied to this repo's GitHub Actions OIDC identity via
  Sigstore (Fulcio/Rekor), not a stored private key, so there's no signing
  secret to leak or rotate.
- **[SLSA build provenance](https://slsa.dev/)** (via GitHub's native
  `attest-build-provenance`) -- what workflow, what commit, what inputs
  produced this exact image.

Verify the image signature without trusting anything but Sigstore's public
transparency log:

```bash
cosign verify \
  --certificate-identity "https://github.com/galleon/openbouncer/.github/workflows/docker-publish.yml@refs/heads/main" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  ghcr.io/galleon/openbouncer:latest
```

Fetch the signed SBOM itself and inspect it (`cosign verify-attestation
--type cyclonedx` is the "should work" command here, but as of cosign
v3.1, its predicate-type matching only looks at the same OCI-referrers
store `attest-build-provenance` writes the SLSA predicate to, not the
tag-based store `cosign attest` uses for the SBOM -- this pulls the raw
signed attestation and extracts the predicate directly instead of relying
on that lookup):

```bash
cosign download attestation ghcr.io/galleon/openbouncer:latest \
  | jq -r 'select(.payload) | .payload | @base64d | fromjson |
      select(.predicateType == "https://cyclonedx.org/bom") | .predicate'
```

And GitHub's native attestation verification (checks the SLSA provenance specifically):

```bash
gh attestation verify oci://ghcr.io/galleon/openbouncer:latest --owner galleon
```

This is the same "don't just claim it, make it checkable" posture as
[sovereignty routing](#sovereignty-routing) and [zero-retention
logging](#guardrail-decision-log) -- see [SOVEREIGN.md](SOVEREIGN.md).

## Authentication

Every `/v1/*` endpoint requires an API key via `Authorization: Bearer <key>`
(`/healthz` does not). Keys are configured the same way as the model
registry -- YAML, sourced from a file or an environment variable -- and only
a **SHA-256 hash** of each key is ever stored or held in memory, never the
raw key.

Config source (first match wins), same convention as the model registry:

1. `OPENBOUNCER_API_KEYS_YAML` -- inline YAML content.
2. `OPENBOUNCER_API_KEYS_CONFIG` -- path to a YAML file (raises at startup if the path is set but missing).
3. `config/api_keys.yaml` if present.
4. Otherwise: an **empty key store** (every request gets 401) -- there is no bundled default with real key material, unlike `config/models.yaml`.

Copy `config/api_keys.example.yaml` to `config/api_keys.yaml` (gitignored) to
get started, and generate a real key + hash with:

```bash
python -c "import secrets, hashlib; k = 'sk-' + secrets.token_urlsafe(32); print('key:', k); print('key_hash:', hashlib.sha256(k.encode()).hexdigest())"
```

Give the printed `key:` to the client; only `key_hash:` goes in the config.

Each entry in `keys:` is:

```yaml
keys:
  - id: my-key                         # used in logs/usage accounting, not the secret itself
    key_hash: <sha256 hex digest>
    allowed_models: [nvidia/qwen3.6-nvfp4]   # must be a subset of config/models.yaml's ids
    requests_per_minute: 60              # optional, defaults to 60
    token_budget_daily: 500000           # optional, defaults to unlimited -- see Token budgets below
    token_budget_monthly: 10000000       # optional, defaults to unlimited -- see Token budgets below
    required_sovereignty: {data_residency: EU}  # optional, defaults to unrestricted -- see Sovereignty routing below
    is_admin: false                      # optional, defaults to false -- see Admin API below
    admin_scopes: [metrics:read]         # optional, defaults to [] -- see Scoped admin access below
    allowed_guardrails_configs: [self_check_input]  # optional, defaults to unrestricted -- see Admin API below
```

### Sovereignty routing

`allowed_models` says *which* models a key may request; `required_sovereignty`
says *what those models must be* -- a key/tenant that declares e.g.
`required_sovereignty: {data_residency: EU}` gets a real `403`
(`error.code: sovereignty_violation`) for any allowed model that doesn't
carry a matching `data_residency: EU` tag, checked in
`app.auth.dependency.ensure_sovereignty_allowed` right alongside
`ensure_model_allowed`, before any upstream call (including for streaming
requests, same as the prompt-injection pre-filter).

Model entries in `config/models.yaml` carry the other half via an optional
`sovereignty:` map:

```yaml
- id: local/gemma4-nvfp4
  # ...
  sovereignty:
    hosting: on-prem
    data_residency: EU
```

**Deliberately untyped** -- not a fixed `data_residency`/`hosting`/
`provider_country` schema. "Data residency" (where data is physically
stored) and "data sovereignty" (who has legal control over it) are
distinct legal concepts, and different compliance frameworks disagree on
the right taxonomy; this codebase has no authority to encode one as
correct; it just checks whatever tags an operator assigns for an exact
match. A model with no `sovereignty:` tags declared at all fails any key
with a `required_sovereignty` constraint -- absence is never treated as an
implicit match, and every one of a key's required tags must match, not
just some.

### Rate limiting

Per-key `requests_per_minute` is enforced by a fixed-window counter
(`app/auth/rate_limiter.py`) -- MVP-level: it resets every 60s per key and
doesn't smooth bursts at a window boundary. By default this counter is
in-memory and per-process; set `REDIS_URL` (e.g. `redis://redis:6379/0`) to
back it with Redis instead, so multiple gateway replicas share one budget
per key (see [Running with Docker](#running-with-docker)).

With `REDIS_URL` set, `RedisRateLimiter.check` -- called on every
authenticated request via `require_api_key` -- **fails open** if Redis is
unreachable or errors: the request is allowed through and a warning is
logged, rather than every request failing with a 500 for the duration of a
Redis outage. Rate limiting is a best-effort abuse guard here, not a
security boundary, so trading strict enforcement for availability during
an outage is a deliberate choice.

### Usage accounting

`app/auth/usage.py` keeps a per-key counter (`requests`, `prompt_tokens`,
`completion_tokens`, `total_tokens`) -- in-memory and per-process by
default, or backed by Redis (same `REDIS_URL` as [rate
limiting](#rate-limiting)) so multiple replicas share one running total per
key instead of each keeping its own. Either way it's still not
billing-grade metering: it doesn't persist a historical/queryable ledger,
just a running total. Same fail-open posture as rate limiting: if Redis is
unreachable, `RedisUsageTracker.record` logs a warning and drops the
write rather than raising -- a request that already succeeded (the
upstream model already answered) must not turn into a 500 just because
the accounting write failed at the very end. `.get`/`.all` degrade the
same way, returning zeroed/partial stats instead of raising.

Where the numbers come from:

- **Non-streaming, no guardrails**: exact usage from the upstream
  response.
- **Non-streaming, guardrails (`nemo_library`)**: a word-count estimate
  (`len(text.split())`), since nemoguardrails doesn't expose the
  underlying LLM's real token counts through `generate_async()`.
- **Streaming, no guardrails**: `UpstreamClient.stream_chat_completion`
  always sets `stream_options: {"include_usage": true}` on the *upstream*
  request, regardless of whether the caller of this gateway asked for
  one, so it can capture upstream's real final usage chunk for accounting
  even when the calling client didn't opt in. That extra chunk is only
  relayed to the caller if their own request set
  `stream_options.include_usage: true` (matching what a client library
  actually expects to receive); otherwise it's captured and swallowed.
  Not every upstream honors `stream_options` (e.g. Ollama's OpenAI-compat
  layer doesn't) -- token counts just stay `0` in that case, same as
  before this existed, with `requests` still incremented.
- **Streaming, guardrails (`nemo_library`)**: the same word-count estimate
  as the non-streaming guardrails case, computed once the stream
  completes (`NemoLibraryGuardrailsService._stream_response`) -- an
  interrupted/errored stream records no usage at all rather than a
  fabricated success-shaped estimate.

### Token budgets

`requests_per_minute` caps *how often* a key can call the gateway;
`token_budget_daily`/`token_budget_monthly` (`app/auth/budget.py`) cap how
much it can actually *consume* -- both optional, both default to
unlimited (same "opt-in cap" posture as `allowed_guardrails_configs`).
Extends the rate-limiting story from request-rate to actual cost, the
thing an operator running a shared deployment usually cares about more
than raw request counts.

Structurally this can't work exactly like rate limiting: a request's
token cost isn't known until the upstream model has actually generated a
response, so there's no way to charge a request against its budget before
running it. What's checked before running it (in `require_api_key`,
alongside the rate-limit check) is whether the key has *already* exceeded
its budget from prior requests this window -- if so, `429`
(`error.type: rate_limit_error`, `error.code: token_budget_exceeded`).
This is a `429`, not OpenRouter's `402`, matching this codebase's existing
`rate_limit_exceeded` and OpenAI's own real-API convention of returning
`429` for quota exhaustion (`insufficient_quota`), so an OpenAI-compatible
client's existing `429` retry/backoff handling already does something
sane here.

Windows are calendar-aligned (UTC midnight / UTC 1st-of-month), not a
rolling N seconds from first use -- a "daily"/"monthly" budget is meant
to reset predictably, matching OpenRouter's own "daily, weekly, or
monthly reset windows" framing for the same concept. Same in-memory vs.
Redis (`REDIS_URL`) split, and the same fail-open-on-Redis-outage posture
for both `check` and `record`, as [rate limiting](#rate-limiting) and
[usage accounting](#usage-accounting) -- a token budget is a cost-control
guard, not a security boundary. The in-memory tracker's per-window
counters aren't cleaned up on their own (unlike the Redis-backed version,
whose keys carry a TTL slightly longer than their window), so they grow
for the life of the process -- fine at realistic key counts, a known
limitation for a very long-lived, very high-key-count single-process
deployment.

### Request logging

Every request gets a `request_id` (`app/core/logging_middleware.py`), echoed
back as the `X-Request-Id` response header and included in access/auth/usage
log lines, so a single request can be traced across auth failures, rate
limiting, and usage accounting.

### Errors

| Situation | Status | `error.type` | `error.code` |
| --- | --- | --- | --- |
| No `Authorization` header (or malformed) | 401 | `authentication_error` | `missing_api_key` |
| Key doesn't match any configured hash | 401 | `authentication_error` | `invalid_api_key` |
| Over `requests_per_minute` | 429 | `rate_limit_error` | `rate_limit_exceeded` |
| Over `token_budget_daily`/`token_budget_monthly` (see [Token budgets](#token-budgets)) | 429 | `rate_limit_error` | `token_budget_exceeded` |
| Model not in the key's `allowed_models` | 403 | `permission_error` | `model_not_allowed` |
| Model doesn't meet the key's `required_sovereignty` (see [Sovereignty routing](#sovereignty-routing)) | 403 | `permission_error` | `sovereignty_violation` |
| `guardrails.config_id` not in the key's `allowed_guardrails_configs` | 403 | `permission_error` | `guardrails_config_not_allowed` |
| Blocked by the [prompt injection detection](#prompt-injection-detection) guardrail | 403 | `permission_error` | `prompt_injection_detected` |
| Blocked by the [output-leak guardrail](#output-leak-guardrail) (non-streaming only -- see that section for streaming) | 403 | `permission_error` | `output_leak_detected` |
| `/api/admin/*` (or `/metrics`) called by a non-admin key | 403 | `permission_error` | `admin_required` |

## Admin API

Lets an `is_admin: true` key manage, over HTTP, things that otherwise
require hand-editing config files (and, for new/rotated/deleted keys, a
restart): the full lifecycle of other keys (create, edit, rotate,
delete), which guardrails configs each key is allowed to request, and the
structured (bullet-list) parts of the bundled guardrails presets.

### Guardrails config access per key

`allowed_guardrails_configs` on a key entry (see
[Authentication](#authentication)) works like a nullable version of
`allowed_models`:

- **Omitted / `null`** (the default -- every key had this behavior before
  the field existed): unrestricted. The key can set
  `guardrails.config_id` to any config_id that exists.
- **A list** (possibly empty): the key may only use `guardrails.config_id`
  values in that list. An empty list means "no guardrails configs at all."

This check only applies when a request *explicitly* sets
`guardrails.config_id` -- a request that omits it and relies on the
server-side `GUARDRAILS_NEMO_DEFAULT_CONFIG_ID` default isn't affected,
since that default is an operator choice, not something the client picked.

### Scoped admin access

`is_admin: true` grants every admin capability. For a key that should only
have *some* of them -- e.g. a Prometheus scrape credential that only needs
to read `/metrics`, or an SRE who should see the Activity dashboard but
must not be able to mint/delete keys or rewrite guardrails policy -- set
`admin_scopes` on the key entry instead, to a subset of:

| Scope | Grants |
| --- | --- |
| `keys:write` | Every `/api/admin/keys*` endpoint: full key lifecycle. |
| `guardrails:write` | `/api/admin/guardrails/configs*`: list + edit the structured parts of bundled presets. |
| `prompt_injection:write` | `/api/admin/prompt-injection*`: read/edit/test the prompt-injection config. |
| `output_leak:write` | `/api/admin/output-leak*`: read/edit/test the [output-leak guardrail](#output-leak-guardrail) config. |
| `metrics:read` | `GET /metrics`. |
| `activity:read` | `GET /api/admin/activity/overview`, `GET /api/admin/guardrail-events`, and `POST /api/admin/alerts/test`. |

`admin_scopes` is ignored (every scope is implied) on a key with
`is_admin: true`; it only matters for a key that isn't a full admin. Create
or update a scoped key the same way as any other, via the [key-lifecycle
endpoints](#endpoints) below (`POST /api/admin/keys` /
`PATCH /api/admin/keys/{key_id}`) -- `admin_scopes` is just another field
on the request body, e.g. `{"id": "prometheus-scraper", "allowed_models":
[], "admin_scopes": ["metrics:read"]}`. `GET /api/ui/whoami` reports the
*effective* scope set (already expanded for `is_admin: true` keys), which
the admin/activity web UI uses to show only the sections a scoped key can
actually use.

### Enabling admin access

Add a key with `is_admin: true` to `config/api_keys.yaml` (see
[Authentication](#authentication) for the key-generation command) and
restart the gateway. That restart is only needed for this first,
hand-edited admin key -- every write made *through* the admin API below
(creating, editing, rotating, or deleting a key; editing a guardrails
config) clears the in-process key-store cache as part of the write, so
it's live on the very next request, no restart. `config/` and
`guardrails_configs/` are already read-write in this deployment's
`docker-compose.yml` (see [Running with Docker](#running-with-docker)),
so admin writes just work, no extra opt-in needed here.

### Endpoints

All under `/api/admin/*`, each requiring the specific [admin
scope](#scoped-admin-access) noted below (an `is_admin: true` key
satisfies all of them):

| Method | Path | Scope | Does |
| --- | --- | --- | --- |
| GET | `/api/admin/keys` | `keys:write` | List all keys (id, `is_admin`, `admin_scopes`, `allowed_models`, `allowed_guardrails_configs`, `requests_per_minute` -- never `key_hash`). |
| POST | `/api/admin/keys` | `keys:write` | Create a new key: `id`, `allowed_models`, and optionally `requests_per_minute`, `is_admin`, `admin_scopes`, `allowed_guardrails_configs`. The raw key is generated server-side and returned exactly once in the response (`api_key`) -- only its hash is ever persisted, so this is the only chance to see it. |
| PATCH | `/api/admin/keys/{key_id}` | `keys:write` | Partially update `allowed_models`, `requests_per_minute`, `is_admin`, and/or `admin_scopes` -- omitted fields are left alone. (`allowed_guardrails_configs` has its own endpoint, below.) |
| POST | `/api/admin/keys/{key_id}/rotate` | `keys:write` | Issue a new raw key for an existing id, replacing its stored hash. The old raw key stops working immediately; the new one is returned once, same as create. |
| DELETE | `/api/admin/keys/{key_id}` | `keys:write` | Revoke a key permanently. |
| PATCH | `/api/admin/keys/{key_id}/guardrails-configs` | `keys:write` | Set a key's `allowed_guardrails_configs` to an explicit list. |
| GET | `/api/admin/guardrails/configs` | `guardrails:write` | List every discovered `config_id`, plus (for the bundled presets listed in `EDITABLE_CONFIG_MANIFEST`) its current structured, editable fields. |
| PATCH | `/api/admin/guardrails/configs/{config_id}` | `guardrails:write` | Update those structured fields (e.g. `topic_safety`'s allowed-topics list). Persists to `guardrails_configs/<id>/config.yml` and invalidates that config's in-process cache, so the new rules apply on the very next request. |
| GET | `/api/admin/prompt-injection` | `prompt_injection:write` | Current [prompt injection detection](#prompt-injection-detection) config: `enabled`, `scope`, `detect_evasions`, `allow_list`, and `categories` (all 9, each an action). |
| PATCH | `/api/admin/prompt-injection` | `prompt_injection:write` | Partially update the above -- `categories` is itself a partial map (only the keys being changed; others are left alone), merged against the on-disk value. Persists to `config/prompt_injection.yaml` and invalidates the in-process cache, live on the very next request. |
| POST | `/api/admin/prompt-injection/test` | `prompt_injection:write` | Scan sample `text` against the *currently saved* config (works even while `enabled: false`, and never persists anything) -- returns the resolved action, every matching pattern, and a redacted preview when the action is `redact`. |
| GET | `/api/admin/output-leak` | `output_leak:write` | Current [output-leak guardrail](#output-leak-guardrail) config: `enabled`, `allow_list`, `categories` (all 6 fixed categories, each an action), `custom_patterns`. |
| PATCH | `/api/admin/output-leak` | `output_leak:write` | Partially update the above -- `categories` is a partial map (merged against the on-disk value, same as prompt-injection's); `custom_patterns`, if given, fully replaces the list. Persists to `config/output_leak.yaml` and invalidates the in-process cache, live on the very next request. |
| POST | `/api/admin/output-leak/test` | `output_leak:write` | Scan sample `text` against the *currently saved* config (works even while `enabled: false`, and never persists anything) -- returns the resolved action, every matching pattern, and a redacted preview when the action is `redact`. |
| GET | `/api/admin/activity/overview` | `activity:read` | Prometheus-backed traffic/usage summary for the [Observability](#observability) dashboard. |
| GET | `/api/admin/guardrail-events` | `activity:read` | Filterable list of individual prompt-injection/output-leak decisions -- see [Guardrail decision log](#guardrail-decision-log). |
| POST | `/api/admin/alerts/test` | `activity:read` | Send a test payload to the configured alert webhook and report the outcome -- see [Alerting](#alerting). Never persists anything. |
| GET | `/metrics` | `metrics:read` | Prometheus exposition format -- see [Metrics](#metrics). |
| GET | `/api/admin/audit-log` | full `is_admin: true` | List recent admin writes, most recent first (`?limit=`, default 50, max 200) -- see [Audit log & revert](#audit-log--revert). |
| POST | `/api/admin/audit-log/{entry_id}/revert` | full `is_admin: true` | Undo one entry: restores the exact file content from just before that write. |

`PATCH`/`DELETE` on `/api/admin/keys/{key_id}` refuse a change that would
leave **no key at all** with `keys:write` access (via `is_admin: true` or
an explicit `admin_scopes` grant) -- `409 cannot_remove_last_admin_key` --
since that scope is the one that can always regrant every other admin
capability, so losing it completely is unrecoverable without hand-editing
`config/api_keys.yaml` on the host (see
`app.auth.keys.LastKeysWriteAdminError`). This only guards the *last*
`keys:write` key, though: with two or more such keys, you can still
demote/delete/rotate the one you're currently authenticated with, same
sharp edge as always.

### Audit log & revert

Every write made through the admin API -- key create/update/rotate/delete,
a guardrails config edit, a prompt-injection config edit, an output-leak
config edit -- is recorded to
`config/audit_log.jsonl` (one JSON object per line, append-only) by
`app/core/audit.py`, capturing who (`actor_key_id`), when, what changed
(`action`/`summary`), and the *exact full prior text* of the file it wrote
(`before`). That `before` snapshot is the entire revert mechanism: reverting
an entry just writes it back to the same file and records the revert itself
as a new entry, so the log stays a true history rather than something
that's edited after the fact.

`GET /api/admin/audit-log` and `POST
/api/admin/audit-log/{entry_id}/revert` both require a full `is_admin: true`
key, not just any admin scope -- see `require_full_admin` in
`app/auth/dependency.py`. This is the one deliberate exception to the
[scoped admin access](#scoped-admin-access) model: the audit log spans
every resource type (keys, guardrails configs, prompt-injection config),
so "some admin scope" isn't a coherent boundary for who gets to see or
undo changes outside their own area.

**Retention:** the log is bounded to the most recent
`OPENBOUNCER_AUDIT_LOG_MAX_ENTRIES` entries (default `5000`) -- once a
write pushes it over that, `record_entry()` trims the oldest entries off
the front of the file. An entry that's aged out this way can no longer be
listed, fetched, or reverted; there's no external log rotation to
configure separately.

Only the bundled presets listed in `EDITABLE_CONFIG_MANIFEST`
(`app/guardrails/editable_config.py` -- see `guardrails_configs/README.md`
for the current list and what each one does) are structurally editable --
each is edited via a fixed set of named sections (one per policy/topic/
pattern list), not raw YAML, so an admin can't produce an invalid config. Adding a
brand-new `config_id` from scratch isn't supported by this API; add one by
hand under `guardrails_configs/` the normal way (see [`nemo_library`
mode](#nemo_library-mode)) -- once discovered, an admin can grant keys
access to it even though its content stays hand-edit-only.

There's also a small admin panel at `/ui/admin.html` (alongside the
existing chat-tester `/ui`) covering all of the above: a create-key form
(with `admin_scopes` checkboxes alongside `is_admin`), a keys table with
editable `allowed_models`/`requests_per_minute`/`token_budget_daily`/
`token_budget_monthly`/`is_admin`/`admin_scopes` (plus Rotate and Delete)
and checkboxes per guardrails config, an editor
per bundled preset with one `<textarea>` per section (one item per line),
a "Prompt Injection" card (enable toggle, scope, one action dropdown per
category, an allow-list textarea, and a "Test your patterns" box that
calls the `/test` endpoint above), an "Output-leak guardrail" card (same
shape, plus an editor for admin-defined custom regex patterns), and an
"Audit log" table (full
`is_admin: true` only) with a Revert button per entry. A newly created or
rotated raw key is shown once in a dismissible callout, mirroring the
API's own one-time-only behavior. The panel calls `GET /api/ui/whoami`
first and renders only the sections the pasted key's `admin_scopes`
actually cover, rather than an all-or-nothing "access denied" -- a key
scoped to just `metrics:read` + `activity:read`, for example, sees none of
this panel's sections at all (there's nothing here it can use), while one
scoped to `guardrails:write` sees a working guardrails editor and nothing
else.

### Tamper-evident audit log

Both the [admin audit log](#audit-log--revert) and the [guardrail decision
log](#guardrail-decision-log) are hash-chained: every entry's `hash` covers
its own content plus the previous entry's `hash` (`prev_hash`), so editing,
deleting, or reordering any past entry breaks every hash from that point
forward. `GET /api/admin/audit-log/verify` (full-admin only, matching the
audit log's own gate) and `GET /api/admin/guardrail-events/verify`
(`activity:read`, matching that log's own gate) walk the whole file and
report `{valid, verified_count, legacy_unchained_count, broken_at_id,
broken_reason}`. The same check also runs offline, without the server
running, against either log file directly:

```
uv run python -m app.core.hash_chain config/audit_log.jsonl
```

**What this proves, and what it doesn't.** This detects internal
inconsistency: nothing in the file changed after the fact without leaving a
gap a reviewer can point to. It does **not** protect against someone who
has filesystem write access to the log *and* understands this mechanism --
that person can regenerate the whole chain from scratch, consistently. Real
protection against that needs external anchoring (publishing periodic root
hashes somewhere this project doesn't control), which isn't implemented.
This is the same class of claim as [Supply chain](#supply-chain)'s
signatures: checkable, not a promise trusted on faith -- but checkable
against *this file*, not against an outside authority.

Both logs also trim their oldest entries once they exceed their retention
cap (see [Audit log & revert](#audit-log--revert)'s and [Guardrail decision
log](#guardrail-decision-log)'s retention notes) -- each trim writes a small
sidecar checkpoint (`*.chain_checkpoint.json`, alongside the `.jsonl` file)
recording the hash of the last entry evicted, so `verify` can confirm the
kept entries genuinely continue that chain rather than treating the trim as
an unverifiable gap.

Deployments upgrading from a version before this feature existed have
existing log lines with no `hash`/`prev_hash` at all. Those aren't
retroactively hashed -- doing so would fabricate a chain across history this
code can't actually vouch for. The first entry written after upgrading
starts a fresh chain, and `verify` reports the pre-existing lines as
`legacy_unchained_count` rather than passing or failing them.

### Multi-replica deployments

[Rate limiting](#rate-limiting), [usage accounting](#usage-accounting),
[token budgets](#token-budgets), and [alerting](#alerting)'s burst
counter/cooldown are all safe to run with multiple gateway replicas --
that's exactly what their shared `REDIS_URL` backing exists for, and
Redis genuinely coordinates across processes.

**Admin writes need the same `REDIS_URL`, plus a shared `config/`.**
`config/api_keys.yaml`, `config/prompt_injection.yaml`, guardrails config
edits, and the two hash-chained logs (`config/audit_log.jsonl`,
`config/guardrail_events.jsonl`) are still plain files on local disk, but
every write to them now goes through
`app.core.distributed_lock.admin_write_lock(name)` -- one named lock per
resource (`api_keys`, `prompt_injection`, `output_leak`,
`guardrails_config:{config_id}`, `audit_log`, `guardrail_events`), backed
by a real Redis-coordinated lock when `REDIS_URL` is set, falling back to
the same process-local `asyncio.Lock` as before when it isn't. Two things
are both still required for correctness with more than one replica:

- **A shared `config/` volume across replicas.** The lock only prevents
  two replicas from racing on the *same* file -- it does nothing if each
  replica has its own local `config/` and never sees the other's writes at
  all. This isn't set up by default under `docker-compose.admin.yml` with
  more than one `gateway` container; without it, replicas still silently
  diverge regardless of locking.
- **`REDIS_URL` set on every replica.** Without it, `admin_write_lock`
  degrades to a process-local `asyncio.Lock` -- correct for a single
  replica (unchanged from before), but back to the original race condition
  across two.

With both in place, concurrent admin writes across replicas are actually
serialized, including the two hash-chained logs' append ordering (so
[verify](#tamper-evident-audit-log) doesn't misreport a genuine
multi-replica race as tampering). `admin_write_lock` fails **closed**, not
open, on a Redis error -- unlike rate limiting/usage/budget/alerting, an
admin write racing unprotected is a silent lost update, not a rare
inconvenience, so a lock that can't actually coordinate raises
`AdminWriteLockUnavailableError` (surfaced as `503
admin_write_lock_unavailable`) rather than silently letting the write
through unprotected.

## Observability

### Metrics

`GET /metrics` (Prometheus exposition format, gated by the `metrics:read`
[admin scope](#scoped-admin-access)) covers the gateway itself. Mint a key
with just `admin_scopes: [metrics:read]` for the scrape config instead of
reusing a full `is_admin: true` key -- request-rate-by-key is sensitive
usage data, but a scrape credential has no need for the ability to also
mint/delete keys or rewrite guardrails/prompt-injection policy.

- `openbouncer_http_requests_total` / `openbouncer_http_request_duration_seconds`
  -- every request, labeled by method/route-template/status. Labeled by
  the *matched route template* (e.g.
  `/api/admin/keys/{key_id}/guardrails-configs`), not the raw request
  path, so path parameters and garbage/scanner paths that never match a
  route can't create unbounded label cardinality.
- `openbouncer_chat_completions_total` / `openbouncer_chat_completion_duration_seconds`
  -- per model. For streaming requests the duration covers the *whole*
  stream (measured when it actually closes), not just time-to-first-chunk.
  Only recorded once `request.model` is confirmed to exist in the
  registry, for the same cardinality reason as above.
- `openbouncer_guardrails_requests_total{config_id}` -- requests processed
  by each guardrails config. No `blocked` label: nemoguardrails doesn't
  expose a reliable, config-agnostic "was this blocked" signal through the
  plain `generate_async()` call this codebase uses (see the comment in
  `app/core/metrics.py` for what a real fix would need).
- `openbouncer_prompt_injection_scanned_total` /
  `openbouncer_prompt_injection_matches_total{category,via}` /
  `openbouncer_prompt_injection_actions_total{action}` -- the standalone
  [prompt injection detection](#prompt-injection-detection) pre-filter:
  how many requests it scanned, every match found (by category and
  detection path), and the action actually applied per request.
- `openbouncer_output_leak_scanned_total` /
  `openbouncer_output_leak_matches_total{category}` /
  `openbouncer_output_leak_actions_total{action}` -- the standalone
  [output-leak guardrail](#output-leak-guardrail) post-filter: same shape
  as the prompt-injection metrics above, but for responses instead of
  requests. Admin-defined `custom_patterns` matches are grouped under
  `category="custom"` rather than their own name, for the same
  cardinality reason `app/core/metrics.py` documents elsewhere.
- `openbouncer_tool_calls_total{model}` -- non-streaming
  [tool-calling](#tool-calling) responses containing at least one tool
  call. Not measured for streaming responses.
- `openbouncer_alerts_triggered_total{guardrail}` /
  `openbouncer_alert_webhook_failures_total` -- [Alerting](#alerting):
  burst-block alerts fired (by which guardrail's blocks crossed the
  threshold) and webhook deliveries that failed. No `key_id` label
  (unlike the usage gauges below) -- an attacker hammering many distinct
  key_ids could otherwise inflate this counter's cardinality; see the
  guardrail event log for per-key detail instead.
- `openbouncer_model_inflight_requests` / `openbouncer_model_queued_requests`
  -- live view of each model's concurrency limiter (see below).
- `openbouncer_usage_requests_total` / `openbouncer_usage_tokens_total`
  -- the existing per-key `UsageTracker` (see [Usage
  accounting](#usage-accounting)), exported as gauges.

### Guardrail decision log

The metrics above answer *how many* prompt-injection/output-leak matches
happened; `GET /api/admin/guardrail-events` (gated by the `activity:read`
[admin scope](#scoped-admin-access) -- same audience as the Activity
dashboard, not a new dedicated scope) answers *which ones*: a filterable,
most-recent-first list of individual guardrail decisions, each with
`key_id`, `guardrail` (`prompt_injection` or `output_leak`), `model`,
`category`, `pattern_name`, `action`, `via` (prompt-injection only), a
`snippet`, and `request_id` -- enough to actually investigate a specific
incident instead of only seeing an aggregate counter move. Query params:
`limit` (default 50, max 200), and optional exact-match filters `key_id`,
`guardrail`, `action`.

Recorded by `app/core/guardrail_events.py` -- one entry per *match*, not
per request (a request with three flagged categories gets three entries),
and regardless of which action (`flag`/`redact`/`block`) actually applies,
so a category left at the default `flag` still shows up here even though
nothing about the request was changed. Same append-only-JSONL-with-a-
retention-cap shape as the [admin audit log](#audit-log--revert)
(`config/guardrail_events.jsonl`, gitignored, `OPENBOUNCER_GUARDRAIL_EVENTS_PATH`
/ `OPENBOUNCER_GUARDRAIL_EVENTS_MAX_ENTRIES` env vars, default cap 20000)
but a deliberately separate file and module: this logs per-request
guardrail activity against *someone else's* traffic, not admin config
writes, so it has no revert semantics and a higher expected write volume.

**Snippet privacy**: `snippet` never contains a raw output-leak match (an
actual email, SSN, API key, ...) -- it's always that category's own
redaction placeholder (e.g. `[EMAIL]`), so this log can't become a second
copy of the very data the output-leak guardrail exists to keep from
leaking further. Prompt-injection snippets are the adversarial *input*
phrase itself (e.g. "ignore all previous instructions") -- not PII, and
exactly what a reviewer needs to tell a real attack from a false
positive, so those are logged as-is *by default*.

That default doesn't hold for every deployment: an attack phrase can
co-occur with real user content in the same message, and some deployments
(classified/regulated input) may not be able to accept *any* raw request
content in a log at all. Setting `OPENBOUNCER_LOG_PROMPT_CONTENT=false`
(default: enabled) makes `record_event()` replace every event's `snippet`
-- prompt-injection included -- with a bracketed category placeholder
(e.g. `[instruction_override]`) instead of the matched text. Enforced
centrally in `app/core/guardrail_events.py`, not per call site, so a new
guardrail added later can't accidentally bypass it. Every other field
(`category`/`pattern_name`/`action`/`via`/`model`/`key_id`/`request_id`)
is classification metadata, not content, and is unaffected -- investigation
still works, it just never has the matched text to show.

The Activity dashboard's "Guardrail events" table (`/ui/activity.html`)
renders this endpoint with the same three filters, and -- unlike the rest
of that page -- doesn't need `PROMETHEUS_URL` configured, since it reads
this log directly rather than querying Prometheus.

Like the admin audit log, this log is hash-chained -- see [Tamper-evident
audit log](#tamper-evident-audit-log).

### Alerting

The guardrail decision log above and the Activity dashboard are both
*pull* -- an admin has to go look. `app/auth/alerting.py` adds a *push*:
when one key's guardrail blocks cross a threshold within a time window, a
webhook fires. Deployment config, not per-request policy -- env-var
driven (like `REDIS_URL`/`PROMETHEUS_URL`), not an admin-API-editable
YAML like the prompt-injection/output-leak configs, since "where to send
ops alerts" is an infrastructure decision, not a guardrail policy a key's
traffic should be evaluated against.

| Env var | Default | Purpose |
| --- | --- | --- |
| `OPENBOUNCER_ALERT_WEBHOOK_URL` | unset (disabled) | Where to `POST` the alert. Unset means the feature does nothing at all -- not even the burst counter runs (see `app.auth.alerting.is_configured`). |
| `OPENBOUNCER_ALERT_BLOCK_THRESHOLD` | 5 | Blocks within the window that trigger an alert. |
| `OPENBOUNCER_ALERT_WINDOW_SECONDS` | 300 | Burst window. |
| `OPENBOUNCER_ALERT_COOLDOWN_SECONDS` | 1800 | Suppresses re-alerting the same key for this long after one fires -- gates *notification* only, not counting: if blocks continue past the cooldown, the next one that crosses the threshold in whatever the current window is fires a fresh alert. |

Counted **per request**, not per matched category (a request with three
blocked categories is one block toward the burst, not three), and
**combined across both guardrails** into one counter per key -- a single
request can only ever hit one of the two block paths (prompt-injection
blocks pre-generation, so output-leak's own check never runs for that
request), so this never double-counts one request under both.

```json
{
  "text": "OpenBouncer: key \"foo\" triggered 5 blocks in 300s (prompt_injection: 3, output_leak: 2)",
  "key_id": "foo",
  "block_count": 5,
  "window_seconds": 300,
  "guardrails": {"prompt_injection": 3, "output_leak": 2},
  "timestamp": "..."
}
```

The `text` field renders directly in a Slack incoming webhook with no
Slack-specific code on this side; the structured fields work for any
other receiver. **Never includes match snippets or content** -- only
counts and categories -- so a webhook pointed at a third party can't
become a second leak of whatever the guardrail just blocked; the actual
matched content stays behind the authenticated `GET
/api/admin/guardrail-events` above.

Delivery is fire-and-forget: the `POST` runs as an independent background
task, not awaited on the request path, so a slow or unreachable webhook
can never add latency to (or fail) the response the caller is actually
waiting for. Single best-effort attempt, no retries -- same fail-open
posture already applied to every Redis-backed tracker in this codebase
(rate limiting, usage, budgets), now extended to webhook delivery: a
failure is logged and counted (`openbouncer_alert_webhook_failures_total`)
and otherwise ignored. With `REDIS_URL` set, the burst counter and the
alert-vs-cooldown decision are both coordinated across replicas the same
way rate limiting/usage/budgets already are (see [Multi-replica
deployments](#multi-replica-deployments)) -- including the cooldown claim
itself, via an atomic `SET NX EX`, so two replicas racing on the same
burst can't both fire.

`POST /api/admin/alerts/test` (gated by `activity:read`, same reasoning
as the guardrail-events endpoint -- there's no "alerting:write" scope
since nothing here is ever persisted) sends a clearly-labeled test
payload to the configured webhook and reports the outcome
(`configured`/`delivered`/`status_code`/`error`) in the response, so an
operator can verify their webhook URL actually works instead of finding
out it was typo'd only when a real burst happens.

### Whole-stack metrics (Prometheus + Grafana)

Beyond the gateway's own `/metrics`, this deployment can scrape the whole
DGX Spark stack at once -- vLLM already exposes Prometheus metrics
out of the box, and Caddy gets a dedicated metrics listener (see
`Caddyfile`, `:9090`, deliberately *not* the admin API, which allows live
reconfiguration and stays bound to `127.0.0.1` inside its container).
Disabled by default:

```bash
docker compose --profile observability up -d
```

Requires, both gitignored/not committed:
- `observability/gateway_admin_key` -- a file containing a raw `is_admin: true`
  API key (Prometheus's own config format doesn't support `${VAR}`
  substitution the way `docker-compose.yml` does, so this has to be a
  file, not an env var -- see `observability/prometheus.yml`).
- `GRAFANA_ADMIN_PASSWORD` in `.env`.

Grafana is at `http://localhost:3000` (loopback-only, same posture as the
gateway's own `:8000` -- not exposed through Caddy by default) with a
provisioned "OpenBouncer" dashboard covering request rate/latency/errors,
per-model concurrency, per-key usage, and guardrails request rate.

### Structured logs

Logs are JSON (one object per line) via a small stdlib-only
`logging.Formatter` in `app/core/logging_config.py`, not the plain
`%(asctime)s ...` text format. This isn't cosmetic: the previous format
string never referenced the `extra={...}` fields
`RequestLoggingMiddleware` already computed (`request_id`, `method`,
`path`, `status_code`, `duration_ms`), so they were silently dropped --
JSON output includes every `extra` field automatically, which is also
what a real log aggregator (Loki, CloudWatch, ELK, ...) actually wants to
ingest. `LOG_LEVEL` still controls verbosity the same way.

### Model concurrency

`concurrency_limit` in `config/models.yaml` is enforced (an
`asyncio.Semaphore` per model, see `app/core/registry.py`), not just
declared -- requests beyond the limit queue rather than being rejected,
so raising it is a capacity change, not a correctness one. Every path
that ends up calling a given model shares that one model's limiter,
including guardrails-routed chat requests: `NemoLibraryGuardrailsService`
still reaches the upstream through its own connection rather than
`UpstreamClient`, but `app/api/routes/chat.py` acquires the same model's
concurrency slot around that call too, so a guardrails-enabled request
and a direct request to the same model count against one shared budget.

## Examples

All examples assume the gateway is running at `http://localhost:8000`
(`docker compose up` or `uv run uvicorn app.main:app`) and that
`OPENBOUNCER_API_KEY` holds a raw key matching a `key_hash` in your API key
config (see [Authentication](#authentication)):

```bash
export OPENBOUNCER_API_KEY=sk-...
```

### List models

Only models in your key's `allowed_models` are returned.

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer $OPENBOUNCER_API_KEY"
```

```json
{
  "object": "list",
  "data": [
    {"id": "nvidia/qwen3.6-nvfp4", "object": "model", "created": 1700000000, "owned_by": "nvidia"},
    {"id": "nvidia/gemme4-nvfp4", "object": "model", "created": 1700000000, "owned_by": "nvidia"},
    {"id": "nvidia/nemotron-vision", "object": "model", "created": 1700000000, "owned_by": "nvidia"}
  ]
}
```

### Non-streaming chat completion

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $OPENBOUNCER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/qwen3.6-nvfp4",
    "messages": [{"role": "user", "content": "What is the capital of France?"}]
  }'
```

`request.model` must have `chat` in its `capabilities` (see [Model
registry](#model-registry)) -- an embeddings-only model (e.g.
`local/bge-m3`) returns a 400 (`error.code: model_does_not_support_chat`)
rather than being forwarded upstream. The web UI's model picker (see [Web
UI](#web-ui)) already filters to chat-capable models for this reason.

### Streaming chat completion

`stream: true` returns Server-Sent Events; `-N`/`--no-buffer` makes curl
print them as they arrive instead of waiting for the connection to close.

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $OPENBOUNCER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/qwen3.6-nvfp4",
    "messages": [{"role": "user", "content": "Write a haiku about the ocean."}],
    "stream": true
  }'
```

```text
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1700000000,"model":"nvidia/qwen3.6-nvfp4","choices":[{"index":0,"delta":{"content":"Waves"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1700000000,"model":"nvidia/qwen3.6-nvfp4","choices":[{"index":0,"delta":{"content":" crash"},"finish_reason":null}]}

data: [DONE]
```

### Image input

`nvidia/nemotron-vision` is the one bundled model with `vision` in its
capabilities. Content is an array of `text` / `image_url` parts, same as
OpenAI's API.

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $OPENBOUNCER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/nemotron-vision",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "What is in this image?"},
          {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}}
        ]
      }
    ]
  }'
```

### Embeddings

Unlike the non-streaming chat example above, this calls a real upstream
model -- `ollama/nomic-embed-text` in the bundled config
(`ollama pull nomic-embed-text`; adjust `id`/`base_url` if you're using a
different embedding model or provider). Embeddings never go through
guardrails -- see [Embeddings](#embeddings) below.

```bash
curl http://localhost:8000/v1/embeddings \
  -H "Authorization: Bearer $OPENBOUNCER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ollama/nomic-embed-text",
    "input": "The quick brown fox jumps over the lazy dog"
  }'
```

```json
{
  "object": "list",
  "data": [{"object": "embedding", "index": 0, "embedding": [-0.0124, 0.0263, "..."]}],
  "model": "ollama/nomic-embed-text",
  "usage": {"prompt_tokens": 11, "total_tokens": 11}
}
```

### Guardrails enabled

This example uses `nemo_library` mode, which runs guardrails in-process
against a local config directory (see [Guardrails](#guardrails) below for
`nemo_microservice` mode instead). Point the gateway at a config directory
and pass the request's `guardrails.config_id`:

```bash
export GUARDRAILS_MODE=nemo_library
export GUARDRAILS_NEMO_LIBRARY_CONFIG_PATH=./guardrails_configs
```

Four working presets already ship in `guardrails_configs/` --
`self_check_input`, `self_check_output`, `self_check_input_output`, and
`topic_safety` -- each using one of [NeMo Guardrails' built-in library rail
types](https://docs.nvidia.com/nemo/guardrails/about-nemo-guardrails-library/rail-types)
against `local/gemma4-nvfp4` as the guardrails LLM (see
`guardrails_configs/README.md` for what each one does, and how to point
them at a different model). Add your own alongside them following the same
layout (see the "nemo_library mode" section below), or copy one of
`tests/fixtures/guardrails_configs/*` to try the mechanism without
configuring a real upstream LLM engine.

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $OPENBOUNCER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "local/gemma4-nvfp4",
    "messages": [{"role": "user", "content": "Hello!"}],
    "guardrails": {"config_id": "self_check_input_output"}
  }'
```

The response is a normal chat completion, except the reply now reflects
whatever that config's rails decided (passed through, rewritten, or a
refusal) instead of calling the model directly. `stream: true` works the
same way -- see the [streaming limitation](#streaming-limitation) note for
how output rails affect streaming latency.

## Model registry

The models the gateway exposes and how to reach their upstream (upstream
model id, base URL, API key env var, capabilities, concurrency limit, and
an optional `sovereignty:` tag map -- see [Sovereignty
routing](#sovereignty-routing)) are loaded from `config/models.yaml`,
overridable via `OPENBOUNCER_MODELS_CONFIG` / `MODEL_CONFIG_PATH` (path to
an alternate YAML file, either name works) or `OPENBOUNCER_MODELS_YAML`
(inline YAML content).

There's no fixed allowlist of ids -- the registry is entirely
operator-defined. `base_url` can point at any OpenAI-compatible endpoint, not
just NVIDIA's cloud API: a local Ollama server, OpenRouter, a self-hosted
vLLM/NIM container, etc., all in the same file. Each entry's `id` is the
name clients request against the gateway; `upstream_model` is the name
actually sent to that entry's `base_url`, so they don't have to match, e.g.:

```yaml
models:
  # ...existing NVIDIA entries...

  # A local Ollama server. Ollama doesn't check the API key at all, so
  # api_key_env just needs to resolve to *some* non-empty value, e.g.
  # `export UPSTREAM_OLLAMA_API_KEY=ollama`. upstream_model must match a
  # model you've pulled locally (`ollama pull llama3.2`). If the gateway
  # itself runs in Docker, use http://host.docker.internal:11434/v1 instead
  # of localhost to reach a server running on the host.
  - id: ollama/llama3.2
    upstream_model: llama3.2
    base_url: http://localhost:11434/v1
    api_key_env: UPSTREAM_OLLAMA_API_KEY
    capabilities: [chat]
    concurrency_limit: 4

  # OpenRouter (https://openrouter.ai) -- needs a real key from
  # openrouter.ai: `export UPSTREAM_OPENROUTER_API_KEY=sk-or-...`.
  # upstream_model uses OpenRouter's own "provider/model" naming.
  - id: openrouter/claude-3.5-sonnet
    upstream_model: anthropic/claude-3.5-sonnet
    base_url: https://openrouter.ai/api/v1
    api_key_env: UPSTREAM_OPENROUTER_API_KEY
    capabilities: [chat, vision]
    concurrency_limit: 4

  # Local vLLM containers on this box (e.g. a DGX host) -- one container per
  # model, each running vLLM's OpenAI-compatible server. Like Ollama, vLLM
  # doesn't check the API key unless the container is started with
  # --api-key, so api_key_env just needs to resolve to some non-empty value:
  # `export UPSTREAM_VLLM_API_KEY=local`.
  - id: local/bge-m3
    upstream_model: BAAI/bge-m3
    base_url: http://vllm-bge-m3:8000/v1
    api_key_env: UPSTREAM_VLLM_API_KEY
    capabilities: [embeddings]
    concurrency_limit: 8
  - id: local/gemma4-nvfp4
    upstream_model: nvidia/Gemma-4-26B-A4B-NVFP4
    base_url: http://vllm-gemma4:8000/v1
    api_key_env: UPSTREAM_VLLM_API_KEY
    capabilities: [chat]
    concurrency_limit: 4
```

All four are already in the bundled `config/models.yaml` as working examples.
Whatever you add here also needs to appear in at least one API key's
`allowed_models` (see [Authentication](#authentication)) before any client
can actually request it.

The `local/*` pair above assumes vLLM is running as the `vllm-bge-m3` /
`vllm-gemma4` services in the bundled `docker-compose.yml` (see next
section) -- if the gateway isn't running inside that same compose network,
swap those hostnames for `localhost:8001` / `localhost:8002` (the
host-mapped ports) instead.

### Serving local models with vLLM (DGX Spark)

`docker-compose.yml` includes two optional services, disabled by default,
that run vLLM's OpenAI-compatible server for local serving on a
[DGX Spark](https://www.nvidia.com/en-us/products/workstations/dgx-spark/):
`vllm-bge-m3` (BGE-M3 embeddings) and `vllm-gemma4` (Gemma-4 26B, NVFP4
quantized, chat). Start them with:

```bash
docker compose --profile vllm up --build
```

This requires the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the host. DGX Spark is a single GB10 chip -- one Blackwell GPU (`sm_121`),
ARM64 Grace CPU, 128GB of memory unified across both -- not a multi-GPU DGX
server, so both services intentionally target GPU `0` and share that one
128GB pool with the host OS; `--gpu-memory-utilization` on each is set
conservatively (`0.1` / `0.5`) rather than vLLM's `0.9` default for that
reason, and running both containers under load at the same time means they
time-share the one GPU (no MIG/MPS here).

**Image compatibility matters here.** Stock `vllm/vllm-openai:latest` does
not run on GB10: its CUDA 12.8 base is one minor version below what
`sm_121`'s FlashInfer JIT compilation needs (12.9+), and the engine crashes
sizing the KV cache. The compose file defaults `VLLM_IMAGE` to NVIDIA's NGC
image, `nvcr.io/nvidia/vllm:26.06-py3`, which does support GB10 -- override
the tag in a `.env` file to match whatever build you have pulled locally:

```bash
VLLM_IMAGE=nvcr.io/nvidia/vllm:26.06-py3
HF_TOKEN=hf_...   # only needed if a model repo is gated
```

Note NGC's `nvcr.io/nvidia/vllm` images use a pass-through entrypoint, so
each service's `command:` spells out the full `vllm serve <model> ...`
invocation -- if you swap `VLLM_IMAGE` for the community `vllm/vllm-openai`
image instead, whose entrypoint already *is* `vllm serve`, drop the leading
`vllm serve` from `command:` or the container will try to run `vllm serve
vllm serve ...`.

NVFP4 also needs a vLLM build with NVFP4/ModelOpt support; adjust or drop the
`--quantization` flag in `vllm-gemma4`'s `command:` to match what your
installed vLLM version expects. Both containers persist downloaded weights
in a shared `huggingface-cache` Docker volume, so a restart doesn't
re-download them.

### Exposing the gateway externally

`docker-compose.yml` includes a `caddy` service that TLS-terminates in front
of `gateway`. It's always on (no profile) since it's meant to be the *only*
externally-reachable service -- `gateway`, `vllm-bge-m3`, and `vllm-gemma4`
all bind to `127.0.0.1` only, so nothing else on the host is reachable over
the LAN or a public interface even if you forward more ports than intended.

Caddy is built from `Dockerfile.caddy` (not the stock `caddy:2-alpine`
image) with the `caddy-dns/gandi` plugin baked in, so it can get a real
Let's Encrypt certificate via a **DNS-01** challenge -- proving domain
ownership through a DNS TXT record instead of a port-80/443 HTTP check. This
matters if ports 80/443 on your network are already claimed by something
else (e.g. a second DGX box sharing the same router): DNS-01 works on any
port, unlike the standard HTTP-01/TLS-ALPN-01 challenges, which are
hardcoded to 80/443 on the public IP and can't be redirected. The compose
file maps Caddy to external port `8443`.

Required in a `.env` file:

```bash
DOMAIN=your-subdomain.example.com   # must be a domain/subdomain on Gandi DNS
GANDI_API_TOKEN=...                 # Gandi account -> Personal access tokens, DNS record permission for the domain
```

Both are required -- `docker compose up` fails fast with a clear error if
either is missing, rather than starting Caddy in a broken state.

You also need to, outside this repo:

1. Point `DOMAIN`'s A/AAAA record at your network's public IP, via Gandi's
   DNS console. If your ISP hands out a dynamic IP (true for most residential
   Free/Freebox connections), keep this updated with dynamic DNS rather than
   a static record.
2. Forward external port `8443` to this host's port `8443` in your router's
   admin UI (for a Freebox: `mafreebox.freebox.fr` -> *Paramètres de la
   Freebox* -> *Mode avancé* -> port redirections).

Once both are in place, `https://your-subdomain.example.com:8443` reaches
the gateway with a browser-trusted certificate that Caddy renews
automatically. `caddy-data`/`caddy-config` Docker volumes persist the
certificate and Caddy's state across restarts.

If you don't need a public, browser-trusted certificate (e.g. testing from
the host itself, or clients that can be configured to trust a custom CA),
you can skip all of the above: set `DOMAIN=localhost` in `.env` (still
required, but no DNS/Gandi setup needed for this value) and remove the
`tls { dns gandi ... }` block from `Caddyfile`, and Caddy falls back to its
own local CA instead of Let's Encrypt.

### Embeddings

`/v1/embeddings` calls a real upstream model, the same way streaming chat
completions do -- it's not a stub. Any registry entry can serve embeddings
as long as its `capabilities` list includes `embeddings`; requesting a model
without that capability returns a 400
(`error.code: model_does_not_support_embeddings`). The bundled config
includes `ollama/nomic-embed-text` as a working example
(`ollama pull nomic-embed-text`).

**Embeddings never go through guardrails, regardless of `GUARDRAILS_MODE`.**
`app/api/routes/embeddings.py` has no dependency on
`app.guardrails.service` at all -- guardrails (content-safety rails, dialog
management) are about moderating *generated conversational text*, which
doesn't apply to a vector representation of the input. This is a deliberate,
permanent architectural boundary, not a currently-missing feature.

## Guardrails

For a separate, always-available local guardrail that needs no LLM call and
has its own Flag/Redact/Block action model, see [Prompt injection
detection](#prompt-injection-detection) below -- independent of
`GUARDRAILS_MODE`, it runs in addition to whatever's configured here, not
instead of it.

Guardrails are configured via `app.guardrails.service.GuardrailsService` and
selected with the `GUARDRAILS_MODE` environment variable:

| Mode | `GUARDRAILS_MODE` value | What it does | Needs the `nemo` extra? |
| --- | --- | --- | --- |
| Disabled | `disabled` (default) | No-op; requests pass straight through to the upstream LLM. | No |
| NeMo Guardrails Microservice | `nemo_microservice` | Calls a separately-running [NeMo Guardrails Microservice](https://catalog.ngc.nvidia.com/orgs/nvidia/teams/nemo-microservices/containers/guardrails) container's OpenAI-compatible `/v1/chat/completions` endpoint -- a plain `httpx` call, no library needed on this side. | No |
| NeMo Guardrails library | `nemo_library` | Runs [nemoguardrails](https://github.com/NVIDIA-NeMo/Guardrails) in-process against locally-loaded configs. | **Yes** |

**`nemoguardrails` is an optional dependency** (`pyproject.toml`'s `nemo`
extra), not installed by default -- it pulls a large NVIDIA/transformer
stack that a minimal or air-gapped deployment shouldn't have to carry just
to run the default guardrails (the always-available [prompt injection
detection](#prompt-injection-detection) and [output-leak
guardrail](#output-leak-guardrail) below need no LLM call and no extra
package at all). Only `nemo_library` mode needs it:

- **Local dev**: `uv sync --extra dev --extra nemo`.
- **Docker**: `docker compose -f docker-compose.yml -f docker-compose.nemo.yml up --build` (see that file's comments). The published `ghcr.io/galleon/openbouncer` image (see [Supply chain](#supply-chain)) does **not** include it.
- Selecting `nemo_library` without the package installed fails clearly at
  first use (`error.code: nemo_library_not_installed`), not with a raw
  import traceback or a startup crash -- `disabled` and `nemo_microservice`
  are entirely unaffected either way, and the admin API's guardrails-config
  editor still works read-only (listing/viewing presets) without it; only
  *saving* an edit to a `nemo_library` preset needs it, since that's what
  validates the edit didn't break the config.

### `nemo_microservice` mode

Runs the guardrails container yourself (e.g. via `docker compose`, image
`nvcr.io/nvidia/nemo-microservices/guardrails:25.12`) and point OpenBouncer at
it:

| Env var | Default | Purpose |
| --- | --- | --- |
| `GUARDRAILS_NEMO_BASE_URL` / `NEMO_GUARDRAILS_BASE_URL` | `http://localhost:8000/v1` | Base URL of the running microservice (either name works). |
| `GUARDRAILS_NEMO_DEFAULT_CONFIG_ID` | unset | Used when a request doesn't specify `guardrails.config_id`. |
| `GUARDRAILS_NEMO_TIMEOUT_SECONDS` | `30` | Request timeout. |

Non-streaming only for now.

### `nemo_library` mode

Runs guardrails in-process via the `nemoguardrails` Python package instead of
a separate container.

| Env var | Default | Purpose |
| --- | --- | --- |
| `GUARDRAILS_NEMO_LIBRARY_CONFIG_PATH` | `./guardrails_configs` | Directory containing one subdirectory per `config_id`, each with its own `config.yml` (and optional Colang flows) -- the same multi-config layout used by `nemoguardrails server` / the microservice container. |
| `GUARDRAILS_NEMO_DEFAULT_CONFIG_ID` | unset | Used when a request doesn't specify `guardrails.config_id`. |

Each `config_id`'s `RailsConfig`/`LLMRails` is loaded once and cached for the
life of the process (constructing one parses Colang flows, validates rails,
and can initialize embeddings for the knowledge base -- too expensive to redo
on every request).

#### Streaming limitation

nemoguardrails can only stream the main LLM's tokens directly to the caller
when a config has **no output rails**. Output rails need the complete bot
message to check it (e.g. "does this response violate policy?"), so there is
nothing to check incrementally -- the whole message must be generated and
validated before OpenBouncer knows whether/what to send back. Concretely,
`NemoLibraryGuardrailsService.stream_chat_completion` picks one of two
strategies per request, based on whether the resolved config has output
rails (`len(config.rails.output.flows) > 0`):

- **No output rails -> true streaming.** Tokens are forwarded to the client
  as `nemoguardrails.stream_async()` produces them. Input/dialog rails still
  ran before the first token, since dialog management happens before the
  main generation call -- only *output* rails require buffering.
- **Output rails configured -> buffered mode.** OpenBouncer calls the normal
  buffered `generate_async()`, waits for the complete (possibly rewritten or
  refused) response, and then sends it to the client as a single SSE content
  chunk followed by the terminal `data: [DONE]`. This is a streaming
  response in shape only, not in latency -- from the client's perspective it
  looks identical to a real stream, just with all the content arriving in
  one chunk instead of many.

This same limitation applies to `nemoguardrails.stream_async()` itself (it
raises `StreamingNotSupportedError` if you try to use it directly against a
config with output rails but `rails.output.streaming.enabled` not set) --
OpenBouncer's buffered fallback exists so that `stream: true` still works for
every guardrails config, rather than surfacing that as an error.

#### Other `nemo_library` limitations

- **Text only.** nemoguardrails' Colang flows and dialog rails operate on
  plain text; any `image_url` content parts in a request are dropped when
  talking to it. `nemo_microservice` mode and guardrails-disabled requests
  are unaffected.
- **No tool-calling.** Same "text only" limitation applies to `tools`/
  `tool_calls` -- see [Tool-calling](#tool-calling). A request with `tools`
  set gets a clean `400` rather than having them silently dropped.
- **System messages.** nemoguardrails does not yet support a `system` role
  message (per its own `LLMRails.generate_async` docstring). OpenBouncer
  passes such messages through unchanged rather than guessing a mapping.
- **In-band stream errors.** As of `nemoguardrails` 0.23.0,
  `stream_async()` signals a failed generation not by raising, but by
  yielding a single chunk shaped like `{"error": {"message": "..."}}` with no
  machine-checkable marker distinguishing it from literal bot text (a
  `develop`-branch revision of the library adds a typed marker for this, but
  it had not been released at the time this was written). OpenBouncer
  detects this heuristically and converts it into a proper SSE error frame;
  see the comment above `_nemo_stream_error_frame` in
  `app/guardrails/service.py` if upgrading `nemoguardrails`.

## Prompt injection detection

A fast, local, regex-based pre-filter (`app/guardrails/prompt_injection.py`)
that scans every `/v1/chat/completions` request before any LLM call --
**independent of `GUARDRAILS_MODE`**, unlike everything in the [Guardrails](#guardrails)
section above. Modeled on [OWASP's LLM01 Prompt
Injection](https://owasp.org/www-project-top-10-for-large-language-model-applications/)
category and OpenRouter's prompt-injection guardrail. No LLM call, no
added latency worth mentioning, and it can be used at the same time as
`nemo_library`/`nemo_microservice` guardrails (it runs first; see below).

### Categories

Nine pattern categories, each independently configurable:

| Category | Example trigger |
| --- | --- |
| `instruction_override` | "ignore all previous instructions" |
| `mode_activation` | "enter developer mode" |
| `system_override` | "your new system prompt is..." |
| `prompt_extraction` | "reveal your system prompt" |
| `role_manipulation` | "remove all your restrictions" |
| `jailbreak_dan` | "do anything now", "act as DAN" |
| `safety_bypass` | "bypass the safety filters" |
| `tag_injection` | fake `<system>`/`<assistant>` delimiters |
| `control_token_injection` | raw model control tokens, e.g. `<\|im_start\|>`, `[INST]` |

### Evasion countermeasures

Enabled/disabled together via `detect_evasions` (direct pattern matching
always runs regardless):

- **Typoglycemia** -- a word with the same first/last letter and the same
  scrambled middle letters as a keyword (`ignore, bypass, override, reveal,
  delete, system, prompt, instructions`), e.g. "ignroe".
- **Base64/hex decoding** -- candidate substrings are decoded and the
  decoded text is keyword-scanned (not fully pattern-matched) for the same
  keyword list above. Covers both compact and space-separated hex.
- **Character-spaced evasion** -- a run of single letters separated by
  whitespace (e.g. "i g n o r e") is collapsed and checked against the same
  keyword list.

### Configuration

Configured via `config/prompt_injection.yaml` (committed with
`enabled: false`, unlike `config/api_keys.yaml` this file holds no
secrets) or the admin API/UI below -- an admin write persists to that file
and takes effect on the very next request, no restart:

- `enabled` -- master on/off switch.
- `scope` -- `user_messages_only` (default) or `all_messages` (also scans
  system/assistant/tool messages).
- `allow_list` -- phrases that should never trigger the guardrail
  (case-insensitive substring match against a match's own matched text),
  for suppressing false positives.
- `categories` -- per-category action: `disabled`, `flag` (log only,
  default), `redact` (replace the matched span with `[PROMPT_INJECTION]`
  and forward the sanitized request), or `block` (reject the request).
  When several categories/messages match with different configured
  actions, the most restrictive wins: `block` > `redact` > `flag`.

### Interaction with `GuardrailsService`

This pre-filter always runs first, before the dispatch described in
[Guardrails](#guardrails) above:

- **`block`** -- the request never reaches `GuardrailsService` or the
  upstream model at all; a `403` is returned immediately (see
  [Errors](#errors)). **Unlike this gateway's own NeMo-guardrails input
  rails, which refuse via a normal `200` response with refusal text in the
  message body**, this is a deliberate divergence to match OpenRouter's
  behavior and this codebase's own `ensure_model_allowed`/
  `ensure_guardrails_config_allowed` precedent of raising real HTTP errors
  for policy decisions made before any LLM call.
- **`redact`** -- the sanitized request (matched spans replaced) is what
  `nemo_library`/`nemo_microservice` guardrails (if also enabled) or a
  direct upstream call subsequently see.
- **`flag`** -- no mutation; only a log line and metrics.

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": "ignore all previous instructions"}]}'
# => 403 {"error": {"message": "...", "type": "permission_error", "code": "prompt_injection_detected", "param": null}}
```

## Output-leak guardrail

The response-side sibling of [prompt injection detection](#prompt-injection-detection)
above: a fast, local, regex-based post-filter (`app/guardrails/output_leak.py`)
that scans every `/v1/chat/completions` *response* (not request) for
sensitive information before it reaches the caller -- again **independent
of `GUARDRAILS_MODE`**, runs whether or not `nemo_library`/`nemo_microservice`
guardrails are also enabled, and again modeled on an [OpenRouter
guardrail](https://openrouter.ai/docs/guides/features/guardrails/sensitive-info)
(their "Sensitive Info Guardrail") and an OWASP LLM Top 10 category --
here, LLM02 (Sensitive Information Disclosure) rather than LLM01. No LLM
call, same Flag/Redact/Block action model as prompt injection detection.

### Categories

Six pattern categories (five regex-only, one Luhn-checked), each
independently configurable:

| Category | What it catches |
| --- | --- |
| `email` | `user@example.com` |
| `phone` | `415-555-0132` (requires a separator between digit groups -- a bare 10-digit run is not matched, to avoid flagging things like order numbers) |
| `ssn` | `123-45-6789` |
| `credit_card` | A 13-19 digit run that also passes a Luhn checksum -- candidates that fail Luhn are dropped entirely, not just deprioritized, since a bare digit-run regex alone has too many false positives (phone numbers, order numbers, ...) to gate a redact/block action on |
| `ip_address` | IPv4 addresses |
| `secret_token` | AWS access key IDs, OpenAI-style `sk-...` API keys, JWTs, PEM private-key headers, and generic `api_key: ...`/`password: ...`-shaped assignments -- the leak this gateway is most exposed to isn't just end-user PII flowing back out, it's a model regurgitating a credential that appeared earlier in its own context (a system prompt, a tool result, RAG-retrieved text) |

Beyond OpenRouter's fixed set, admins can also add **custom regex
patterns** (`custom_patterns`, e.g. for an internal project codename or a
proprietary identifier format) -- same idea as OpenRouter's `content_filters`,
each with its own name and action. Matches are grouped under
`category="custom"` for Prometheus (see [Metrics](#metrics)); the pattern's
own admin-chosen name is used in logs and in its redaction placeholder,
never as a metric label.

Unlike prompt injection detection's single generic `[PROMPT_INJECTION]`
token, `redact` here uses a category-specific placeholder --
`[EMAIL]`/`[PHONE]`/`[SSN]`/`[CREDIT_CARD]`/`[IP_ADDRESS]`/`[SECRET]`, or
`[NAME]` (uppercased) for a custom pattern named `name` -- again matching
OpenRouter's documented behavior.

**Regex/Luhn only, no NLP name/address detection** (OpenRouter's built-in
set also includes beta NLP-based person-name and address detection via
Presidio) -- keeping this a fast, dependency-free pre-filter, matching
OpenRouter's own "use regex-only presets if latency is critical" guidance.

### Configuration

Configured via `config/output_leak.yaml` (committed with `enabled: false`,
holds no secrets) or the admin API/UI below -- an admin write persists to
that file and takes effect on the very next request, no restart:

- `enabled` -- master on/off switch.
- `allow_list` -- phrases that should never trigger this guardrail (e.g. a
  support team's own published contact email), case-insensitive substring
  match against a match's own matched text.
- `categories` -- per-category action: `disabled`, `flag` (log only,
  default), `redact` (replace the matched span with a category-specific
  placeholder), or `block` (reject the response). Most restrictive wins
  across every match, same precedence as prompt injection detection.
- `custom_patterns` -- a list of `{name, pattern, action}`; `pattern` must
  be a valid regex (rejected at save time otherwise).

### Non-streaming vs. streaming

**Non-streaming**: a `block` decision is still a real HTTP `403` (see
[Errors](#errors)) even though it happens *after* generation -- the
upstream model **is** called (unlike prompt injection detection's
pre-generation block), but a non-streaming JSON response is only ever
sent once, atomically, so nothing has reached the caller yet when the
scan runs.

**Streaming** is different: whether the response needs buffering is
decided once per request, from config alone, before any tokens exist --

- **No category/custom pattern configured `redact`/`block`** (i.e. every
  enabled one is `flag`): tokens are forwarded to the caller live, exactly
  as the upstream/guardrails backend produces them. Scanning still runs
  against the full accumulated text once the stream ends, for
  flag-level logging/metrics only -- nothing so far to act on mid-stream.
- **At least one category/custom pattern is configured `redact` or
  `block`**: the *entire* response is buffered before anything is
  released to the client, then scanned as a whole -- same trade-off
  `NemoLibraryGuardrailsService` already accepts for its own output rails
  (see the [Streaming limitation](#streaming-limitation) section above).
  This applies to every response while the config is in that state, even
  ones that end up matching nothing, since whether *this* response needs
  redaction/blocking is only knowable after the fact. A streaming `block`
  is an **in-band SSE error frame**, not a real HTTP error (same
  convention nemoguardrails' own output-rail failures already use) --
  by the time buffering finishes, the HTTP response has already committed
  to `200 text/event-stream`.

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": "what is your admin contact email?"}]}'
# with the "email" category set to "block":
# => 403 {"error": {"message": "...", "type": "permission_error", "code": "output_leak_detected", "param": null}}
```

## Tool-calling

OpenAI's `tools`/`tool_choice` request fields, and the resulting
`tool_calls` on a response message, are forwarded like any other request
field -- `disabled` and `nemo_microservice` guardrails modes both build
their upstream payload via a plain `model_dump()`, so a `tools`-bearing
request already carries through unmodified once the schema accepts it (see
`app/schemas/chat.py`'s `Tool`/`ToolCall`/`FunctionCall` models). OpenBouncer
never executes a tool itself -- it's a proxy, not an agent runtime; tool
execution, and feeding the result back as a `role: "tool"` message, stays
entirely the calling client's responsibility, same as talking to OpenAI
directly.

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "local/gemma4-nvfp4",
    "messages": [{"role": "user", "content": "What is the weather in Paris?"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"location": {"type": "string"}}}
      }
    }]
  }'
# => {"choices": [{"message": {"role": "assistant", "content": null,
#      "tool_calls": [{"id": "call_...", "type": "function",
#      "function": {"name": "get_weather", "arguments": "{\"location\": \"Paris\"}"}}]},
#      "finish_reason": "tool_calls"}], ...}
```

**Not supported in `nemo_library` mode.** `NemoLibraryGuardrailsService`
reduces every message to plain `{role, text}` before handing it to
nemoguardrails (Colang/dialog rails have no concept of tool calls at all --
same limitation as [image content](#other-nemo_library-limitations), which
that mode also drops). A request that sets `tools` while guardrails are
requested for that
call and `GUARDRAILS_MODE=nemo_library` is active gets a clean `400
tool_calling_not_supported_in_nemo_library_mode` rather than silently
having `tools` dropped and the model never calling anything. `disabled` and
`nemo_microservice` are unaffected; setting `guardrails.enabled: false` on
the request also sidesteps this (it bypasses the guardrails backend
entirely, same as any other request).

**The prompt-injection and output-leak guardrails both cover tool-calling
content, not just `content`.** A model regurgitating a leaked secret or PII
into a function argument is exactly as real a risk as regurgitating it into
prose, so the output-leak guardrail scans every `tool_calls[].function.arguments`
string the same way it scans response text -- streaming included, where
arguments arrive as incremental fragments tagged by index and are
accumulated across chunks before scanning (see
`app.guardrails.output_leak.accumulate_tool_call_deltas`). One thing is
deliberately different for a match found inside tool-call arguments:
**`redact` isn't supported there** -- splicing a redaction token into the
middle of a JSON string risks producing invalid JSON the calling client
can't parse -- so a `redact`-configured category matching inside a tool
call's arguments is escalated to `block` for that match instead, never
silently downgraded to `flag`. A streaming `redact` decision driven by a
*different*, clean match in `content` still collapses the response into one
synthetic chunk the same way it already did (see [Non-streaming vs.
streaming](#non-streaming-vs-streaming) above) -- and that reconstruction
includes any tool_calls the model actually made, so collapsing the stream
never silently drops them.

Prompt-injection scanning already covered a tool result being fed back to
the model (a `role: "tool"` message's `content`) before this feature
existed, when `scope: all_messages` is configured -- no separate extension
was needed there. It does not scan the *model's own* `tool_calls` on an
assistant message (that's the model's prior choice being replayed back to
it, not new adversarial input).

`openbouncer_tool_calls_total{model="..."}` (see [Metrics](#metrics))
counts non-streaming responses containing at least one tool call. Not
measured for streaming responses, to avoid adding per-chunk parsing
overhead to every streamed response regardless of whether anything needs
it.
