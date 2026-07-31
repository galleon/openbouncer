# Guardrails configs (nemo_library mode)

Used only when `GUARDRAILS_MODE=nemo_library`. Each subdirectory here is one
`config_id`: its own `config.yml` (and optional Colang flows), following the
same multi-config layout as `nemoguardrails server` / the microservice
container. See the "nemo_library mode" section of the top-level README for
details, and `tests/fixtures/guardrails_configs/` for two minimal examples
built for use with a fake test LLM (not meant to be copied into a real
deployment as-is).

This directory is bind-mounted into the `gateway` container by
`docker-compose.yml`, so configs added here are picked up without rebuilding
the image -- a new `config_id` is loaded (and then cached) the first time a
request actually uses it, no restart needed.

## Bundled presets

Four working presets, each using [NeMo Guardrails' built-in rail
library](https://docs.nvidia.com/nemo/guardrails/about-nemo-guardrails-library/rail-types)
against a local model as the guardrails LLM (`models: [...]`, engine
`openai` pointed at an OpenAI-compatible `base_url` -- works against any
local vLLM/Ollama/etc. server, not just NVIDIA's cloud API):

| `config_id` | Rail type | What it does |
| --- | --- | --- |
| `self_check_input` | Input rail | Blocks disallowed user messages before they reach the model. |
| `self_check_output` | Output rail | Blocks disallowed bot messages before they reach the client. |
| `self_check_input_output` | Input + output | Both of the above together. |
| `topic_safety` | Input rail | Restricts the conversation to an allowed-topics list (edit the `prompts:` section in its `config.yml` to change the topics). |

Not included, and why: `self_check_facts` (hallucination detection) needs a
knowledge base / retrieved chunks to check answers against, which nothing
here provides; `jailbreak_detection`'s heuristic and model-based methods
need `torch` + `transformers` + a downloaded GPT2-large model, real added
weight this project doesn't otherwise need.

To point a preset at a different backend, edit its `config.yml`'s `models:`
block (`parameters.base_url`, `model`, `api_key_env_var`) to match an entry
in `config/models.yaml`.
