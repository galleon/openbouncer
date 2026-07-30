# Guardrails configs (nemo_library mode)

Used only when `GUARDRAILS_MODE=nemo_library`. Each subdirectory here is one
`config_id`: its own `config.yml` (and optional Colang flows), following the
same multi-config layout as `nemoguardrails server` / the microservice
container. See the "nemo_library mode" section of the top-level README for
details, and `tests/fixtures/guardrails_configs/` for two minimal examples.

This directory is bind-mounted into the `gateway` container by
`docker-compose.yml`, so configs added here are picked up without rebuilding
the image.
