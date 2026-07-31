import logging
from pathlib import Path

from fastapi import APIRouter, Depends

from app.auth.dependency import AuthContext, require_admin
from app.auth.keys import (
    AdminPersistenceError,
    KeyStore,
    get_key_store,
    update_key_allowed_guardrails_configs,
)
from app.core.errors import OpenAIError
from app.core.request_context import get_request_id
from app.guardrails.catalog import GuardrailsCatalogService, get_guardrails_catalog_service
from app.guardrails.editable_config import (
    EDITABLE_CONFIG_MANIFEST,
    GuardrailsConfigParseError,
    read_editable_sections,
    write_editable_sections,
)
from app.guardrails.service import CONFIG_ID_PATTERN, GuardrailsService, get_guardrails_service
from app.schemas.admin import (
    AdminEditableSection,
    AdminGuardrailsConfigItem,
    AdminGuardrailsConfigListResponse,
    AdminKeyItem,
    AdminKeyListResponse,
    UpdateGuardrailsConfigRequest,
    UpdateKeyGuardrailsConfigsRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _to_admin_key_item(record) -> AdminKeyItem:
    return AdminKeyItem(
        id=record.id,
        is_admin=record.is_admin,
        allowed_models=list(record.allowed_models),
        allowed_guardrails_configs=(
            list(record.allowed_guardrails_configs)
            if record.allowed_guardrails_configs is not None
            else None
        ),
        requests_per_minute=record.requests_per_minute,
    )


@router.get("/api/admin/keys", response_model=AdminKeyListResponse)
async def list_keys(
    key_store: KeyStore = Depends(get_key_store),
    auth: AuthContext = Depends(require_admin),
) -> AdminKeyListResponse:
    return AdminKeyListResponse(keys=[_to_admin_key_item(record) for record in key_store.all()])


@router.patch("/api/admin/keys/{key_id}/guardrails-configs", response_model=AdminKeyItem)
async def update_key_guardrails_configs(
    key_id: str,
    body: UpdateKeyGuardrailsConfigsRequest,
    catalog: GuardrailsCatalogService = Depends(get_guardrails_catalog_service),
    auth: AuthContext = Depends(require_admin),
) -> AdminKeyItem:
    known_config_ids = {summary.config_id for summary in catalog.list_configs()}
    for config_id in body.allowed_guardrails_configs:
        if not CONFIG_ID_PATTERN.match(config_id):
            raise OpenAIError(
                f"Invalid guardrails config_id `{config_id}`.",
                status_code=422,
                error_type="invalid_request_error",
                param="allowed_guardrails_configs",
                code="invalid_config_id",
            )
        if config_id not in known_config_ids:
            raise OpenAIError(
                f"Unknown guardrails config_id `{config_id}`.",
                status_code=422,
                error_type="invalid_request_error",
                param="allowed_guardrails_configs",
                code="guardrails_config_not_found",
            )

    try:
        record = await update_key_allowed_guardrails_configs(
            key_id, body.allowed_guardrails_configs
        )
    except KeyError:
        raise OpenAIError(
            f"Unknown key id `{key_id}`.",
            status_code=404,
            error_type="invalid_request_error",
            code="key_not_found",
        ) from None
    except AdminPersistenceError as exc:
        raise OpenAIError(
            str(exc),
            status_code=409,
            error_type="api_error",
            code="key_store_not_file_backed",
        ) from exc

    logger.info(
        "admin key_id=%s granted key_id=%s guardrails configs=%s [request_id=%s]",
        auth.key_id,
        key_id,
        body.allowed_guardrails_configs,
        get_request_id(),
    )
    return _to_admin_key_item(record)


@router.get("/api/admin/guardrails/configs", response_model=AdminGuardrailsConfigListResponse)
async def list_guardrails_configs(
    catalog: GuardrailsCatalogService = Depends(get_guardrails_catalog_service),
    auth: AuthContext = Depends(require_admin),
) -> AdminGuardrailsConfigListResponse:
    config_dir = Path(catalog.config_store_path)
    items: list[AdminGuardrailsConfigItem] = []

    for summary in catalog.list_configs():
        config_id = summary.config_id
        manifest_sections = EDITABLE_CONFIG_MANIFEST.get(config_id)
        if manifest_sections is None:
            items.append(
                AdminGuardrailsConfigItem(config_id=config_id, editable=False, sections=[])
            )
            continue

        try:
            values = read_editable_sections(config_dir / config_id, config_id)
            sections = [
                AdminEditableSection(
                    field=section.field,
                    label=section.label,
                    items=values[section.field],
                )
                for section in manifest_sections
            ]
            items.append(
                AdminGuardrailsConfigItem(config_id=config_id, editable=True, sections=sections)
            )
        except GuardrailsConfigParseError as exc:
            # One hand-mangled config shouldn't 500 the whole list -- report
            # it as editable-but-broken instead.
            items.append(
                AdminGuardrailsConfigItem(
                    config_id=config_id, editable=True, sections=[], error=str(exc)
                )
            )

    return AdminGuardrailsConfigListResponse(configs=items)


@router.patch(
    "/api/admin/guardrails/configs/{config_id}", response_model=AdminGuardrailsConfigItem
)
async def update_guardrails_config(
    config_id: str,
    body: UpdateGuardrailsConfigRequest,
    catalog: GuardrailsCatalogService = Depends(get_guardrails_catalog_service),
    guardrails_service: GuardrailsService = Depends(get_guardrails_service),
    auth: AuthContext = Depends(require_admin),
) -> AdminGuardrailsConfigItem:
    known_config_ids = {summary.config_id for summary in catalog.list_configs()}
    if config_id not in known_config_ids:
        raise OpenAIError(
            f"Unknown guardrails config_id `{config_id}`.",
            status_code=404,
            error_type="invalid_request_error",
            code="guardrails_config_not_found",
        )

    manifest_sections = EDITABLE_CONFIG_MANIFEST.get(config_id)
    if manifest_sections is None:
        raise OpenAIError(
            f"Guardrails config `{config_id}` is not structurally editable.",
            status_code=404,
            error_type="invalid_request_error",
            code="guardrails_config_not_editable",
        )

    config_dir = Path(catalog.config_store_path) / config_id
    try:
        values = write_editable_sections(config_dir, config_id, body.sections, guardrails_service)
    except ValueError as exc:
        raise OpenAIError(
            str(exc),
            status_code=422,
            error_type="invalid_request_error",
            param="sections",
            code="invalid_request_error",
        ) from exc
    except GuardrailsConfigParseError as exc:
        raise OpenAIError(
            str(exc),
            status_code=500,
            error_type="api_error",
            code="guardrails_config_invalid",
        ) from exc

    logger.info(
        "admin key_id=%s edited guardrails config_id=%s [request_id=%s]",
        auth.key_id,
        config_id,
        get_request_id(),
    )

    sections = [
        AdminEditableSection(field=section.field, label=section.label, items=values[section.field])
        for section in manifest_sections
    ]
    return AdminGuardrailsConfigItem(config_id=config_id, editable=True, sections=sections)
