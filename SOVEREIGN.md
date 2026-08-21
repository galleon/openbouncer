# OpenBouncer — Positioning

> The small, fully-inspectable policy layer in front of self-hosted LLMs: guardrails that make zero external calls, sovereignty-tagged model routing, and an audit trail — all in the free repo, none of it gated behind a sales call.

This is a narrower claim than "the sovereign AI gateway." That framing doesn't survive contact with the market — read on for why — and this document exists to replace it with one that does.

## The competitive landscape, correctly stated

| Project | What it actually is | Where it leaves a gap |
|---|---|---|
| **LiteLLM** | Mature, self-hostable proxy — genuinely usable air-gapped, provider breadth, FinOps depth, zero license cost. | No intent-based policy enforcement, no jurisdiction-aware routing, no inference-level audit trail. Guardrails aren't the focus. |
| **Bifrost** (Maxim AI) | A real, well-resourced competitor for exactly this positioning — Go, 7.5k+ stars, actively maintained, 23+ providers, and marketed explicitly around air-gapped AI governance. | The governance features are the pitch, but they're **not in the Apache-2.0 core**: guardrails, OIDC/SSO, Vault-backed secrets, and clustering are all enterprise-gated. And the flagship guardrail mechanism ("Prompt Guardrails") is either an LLM inference call or a call to a third-party cloud moderation API (AWS Bedrock, Azure, Google, Lakera, CrowdStrike, ...) — the opposite of "zero external calls," even before you consider it isn't free. |
| **NeMo Guardrails** | A guardrails library, not a gateway. Requires an LLM call to gate content, pulls a large NVIDIA/transformer stack. | Not deployable on its own; heavy for an air-gapped footprint. |
| **OpenRouter / Helicone / LangSmith / EU-hosting resellers** (Requesty EU, HostYourAI, Tessera AI, ...) | Commercial SaaS, several specifically pitching EU data residency. | Not self-hostable in the sovereign sense — the whole point is that data reaches *their* infrastructure, even if that infrastructure is EU-based. |
| **vLLM / Ollama** | Inference servers. | No auth, no guardrails, no governance, no audit trail — this is what every option above (OpenBouncer included) sits in front of. |

**The corrected claim**: there is no unoccupied niche here — Bifrost occupies almost exactly the position an earlier draft of this document claimed was empty. What's still true, and still differentiating, is narrower: **the actual governance layer — guardrails, policy routing, audit — is free, zero-network, and small enough to read end to end**, where the closest competitor's equivalent is paid, and even paid, routes some of the compliance-critical traffic through third-party APIs it's supposed to be protecting you from.

## The sweet spot

Not "the sovereign AI gateway" — that fight is against a funded, more mature platform, on its own turf (provider breadth, clustering, enterprise identity). Trying to match that feature-for-feature loses.

The defensible claim is narrower and, unlike the market-size argument, is *already true of this codebase* rather than requiring a roadmap to become true:

**OpenBouncer is the governance layer for a team that needs to *prove*, not just claim, that every safety-relevant code path makes no outbound call and is free to read line by line.** Concretely, that's:

- **Guardrails with zero inference, zero network, by construction** (`app/guardrails/prompt_injection.py`, `app/guardrails/output_leak.py`) — regex, Luhn checksums, typoglycemia/base64-decode-then-scan. Not "runtime guardrails" that sometimes call out; never call out. In the free repo, not an enterprise tier.
- **Sovereignty-tagged routing** (`app/auth/dependency.py::ensure_sovereignty_allowed`) — a key can require a model to carry specific provenance tags (e.g. `data_residency: EU`) and gets a real `403 sovereignty_violation` otherwise, enforced before any upstream call. Deliberately untyped tags, not a fixed schema — data *residency* (where stored) and data *sovereignty* (who has legal control) are distinct concepts this codebase has no authority to adjudicate for every jurisdiction, so it carries whatever taxonomy the operator's own compliance framework uses.
- **An audit trail with a documented, readable mechanism** — admin writes (`app/core/audit.py`) and per-request guardrail decisions (`app/core/guardrail_events.py`) are both plain append-only JSONL with a stated retention policy, not an "immutable audit log" whose actual mechanism isn't published anywhere a buyer can check.
- **An explicit zero-retention mode** (`OPENBOUNCER_LOG_PROMPT_CONTENT=false`) for deployments that can't accept *any* raw request content landing in a log, even a narrowly-scoped one.
- **A verifiable build**, not just a verifiable running gateway — every published image (`ghcr.io/galleon/openbouncer`) carries a CycloneDX SBOM, a keyless cosign signature, and SLSA build provenance (see [Supply chain](README.md#supply-chain)), checkable against Sigstore's public transparency log without trusting anything this project says about itself.
- **A minimal default footprint that stays minimal** — `nemoguardrails` (NVIDIA's transformer-based guardrails library) is an optional extra (`uv sync --extra nemo`, `docker-compose.nemo.yml`), not a default dependency. It's needed only for `GUARDRAILS_MODE=nemo_library`; the default `disabled` mode, `nemo_microservice`, and the actual zero-network guardrails above all work — and the published image ships — without it.

**Who this is for**: a team putting a governance layer in front of 1–3 self-hosted models (vLLM/Ollama/NIM), where the actual requirement from security review or procurement is "show me exactly what this calls out to and let me read the code that decides," not "give me a dashboard with 23 providers." That's a smaller buyer than "every sovereign AI deployment," and it's a claim this project can back with source code today, not a roadmap.

## What this means for near-term priorities

- **The free/zero-network guardrail path is the moat — protect it, don't dilute it.** No guardrail capability should move behind a paid tier, ever; that's the one thing the closest competitor doesn't offer, not a placeholder until OpenBouncer can afford to gate it too.
- **Verifiability compounds the claim, and now covers the build too, not just the running gateway.** SBOM/signing/provenance (see [Supply chain](README.md#supply-chain)) turn "you can check this" into a checkable fact for the *artifact*, the same way sovereignty routing and zero-retention logging turn "we don't leak your data" into an enforced, testable behavior for the *code*.
- **Don't chase feature parity on what's enterprise-gated elsewhere** (OIDC/SSO, multi-tenancy, clustering). Building those for free means competing on Bifrost's terms, on infrastructure that's high-blast-radius if done wrong, for a buyer who was never going to choose on that axis anyway.
