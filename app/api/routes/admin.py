import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from pydantic import ValidationError

from app.auth.dependency import AuthContext, require_full_admin, require_scope
from app.auth.keys import (
    ALL_ADMIN_SCOPES,
    DEFAULT_REQUESTS_PER_MINUTE,
    KEY_ID_PATTERN,
    AdminPersistenceError,
    APIKeyRecord,
    KeyAlreadyExistsError,
    KeyStore,
    LastKeysWriteAdminError,
    create_key,
    delete_key,
    generate_api_key,
    get_key_store,
    hash_api_key,
    rotate_key,
    update_key_allowed_guardrails_configs,
    update_key_fields,
)
from app.core.audit import list_entries as list_audit_entries
from app.core.audit import revert_entry as revert_audit_entry_by_id
from app.core.errors import OpenAIError
from app.core.guardrail_events import list_events as list_guardrail_events
from app.core.request_context import get_request_id
from app.guardrails.catalog import GuardrailsCatalogService, get_guardrails_catalog_service
from app.guardrails.editable_config import (
    EDITABLE_CONFIG_MANIFEST,
    GuardrailsConfigParseError,
    read_editable_sections,
    write_editable_sections,
)
from app.guardrails.output_leak import (
    CustomPattern,
    OutputLeakAction,
    OutputLeakCategory,
    OutputLeakConfig,
    get_output_leak_config,
    redact_text as redact_output_leak_text,
    resolve_overall_action as resolve_output_leak_action,
    scan_text as scan_output_leak_text,
    update_output_leak_config,
)
from app.guardrails.prompt_injection import (
    InjectionAction,
    InjectionCategory,
    InjectionScope,
    PromptInjectionConfig,
    get_prompt_injection_config,
    redact_preview,
    resolve_overall_action,
    scan_message,
    update_prompt_injection_config,
)
from app.guardrails.service import CONFIG_ID_PATTERN, GuardrailsService, get_guardrails_service
from app.schemas.admin import (
    AdminAuditEntryItem,
    AdminAuditLogResponse,
    AdminEditableSection,
    AdminGuardrailsConfigItem,
    AdminGuardrailsConfigListResponse,
    AdminKeyItem,
    AdminKeyListResponse,
    CreateKeyRequest,
    CreateKeyResponse,
    GuardrailEventItem,
    GuardrailEventsResponse,
    OutputLeakConfigResponse,
    OutputLeakCustomPatternItem,
    OutputLeakTestMatch,
    OutputLeakTestRequest,
    OutputLeakTestResponse,
    PromptInjectionConfigResponse,
    PromptInjectionTestMatch,
    PromptInjectionTestRequest,
    PromptInjectionTestResponse,
    RevertAuditEntryResponse,
    RotateKeyResponse,
    UpdateGuardrailsConfigRequest,
    UpdateKeyGuardrailsConfigsRequest,
    UpdateKeyRequest,
    UpdateOutputLeakConfigRequest,
    UpdatePromptInjectionConfigRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _to_admin_key_item(record) -> AdminKeyItem:
    return AdminKeyItem(
        id=record.id,
        is_admin=record.is_admin,
        admin_scopes=list(record.admin_scopes),
        allowed_models=list(record.allowed_models),
        allowed_guardrails_configs=(
            list(record.allowed_guardrails_configs)
            if record.allowed_guardrails_configs is not None
            else None
        ),
        requests_per_minute=record.requests_per_minute,
    )


def _validate_admin_scopes(scopes: list[str]) -> None:
    unknown = sorted(set(scopes) - ALL_ADMIN_SCOPES)
    if unknown:
        raise OpenAIError(
            f"Unknown admin scope(s): {unknown}. Must be a subset of {sorted(ALL_ADMIN_SCOPES)}.",
            status_code=422,
            error_type="invalid_request_error",
            param="admin_scopes",
            code="invalid_admin_scope",
        )


@router.get("/api/admin/keys", response_model=AdminKeyListResponse)
async def list_keys(
    key_store: KeyStore = Depends(get_key_store),
    auth: AuthContext = Depends(require_scope("keys:write")),
) -> AdminKeyListResponse:
    return AdminKeyListResponse(keys=[_to_admin_key_item(record) for record in key_store.all()])


@router.post(
    "/api/admin/keys", response_model=CreateKeyResponse, status_code=http_status.HTTP_201_CREATED
)
async def create_key_endpoint(
    body: CreateKeyRequest,
    catalog: GuardrailsCatalogService = Depends(get_guardrails_catalog_service),
    auth: AuthContext = Depends(require_scope("keys:write")),
) -> CreateKeyResponse:
    if not KEY_ID_PATTERN.match(body.id):
        raise OpenAIError(
            f"Invalid key id `{body.id}`.",
            status_code=422,
            error_type="invalid_request_error",
            param="id",
            code="invalid_key_id",
        )

    if body.allowed_guardrails_configs is not None:
        known_config_ids = {summary.config_id for summary in catalog.list_configs()}
        for config_id in body.allowed_guardrails_configs:
            if not CONFIG_ID_PATTERN.match(config_id) or config_id not in known_config_ids:
                raise OpenAIError(
                    f"Unknown guardrails config_id `{config_id}`.",
                    status_code=422,
                    error_type="invalid_request_error",
                    param="allowed_guardrails_configs",
                    code="guardrails_config_not_found",
                )

    _validate_admin_scopes(body.admin_scopes)

    raw_key = generate_api_key()
    record = APIKeyRecord(
        id=body.id,
        key_hash=hash_api_key(raw_key),
        allowed_models=body.allowed_models,
        requests_per_minute=body.requests_per_minute or DEFAULT_REQUESTS_PER_MINUTE,
        is_admin=body.is_admin,
        admin_scopes=body.admin_scopes,
        allowed_guardrails_configs=body.allowed_guardrails_configs,
    )

    try:
        record = await create_key(record, actor_key_id=auth.key_id)
    except KeyAlreadyExistsError:
        raise OpenAIError(
            f"Key id `{body.id}` already exists.",
            status_code=409,
            error_type="invalid_request_error",
            param="id",
            code="key_already_exists",
        ) from None
    except AdminPersistenceError as exc:
        raise OpenAIError(
            str(exc), status_code=409, error_type="api_error", code="key_store_not_file_backed"
        ) from exc

    logger.info(
        "admin key_id=%s created key_id=%s is_admin=%s [request_id=%s]",
        auth.key_id,
        record.id,
        record.is_admin,
        get_request_id(),
    )
    return CreateKeyResponse(key=_to_admin_key_item(record), api_key=raw_key)


@router.patch("/api/admin/keys/{key_id}", response_model=AdminKeyItem)
async def update_key_endpoint(
    key_id: str,
    body: UpdateKeyRequest,
    auth: AuthContext = Depends(require_scope("keys:write")),
) -> AdminKeyItem:
    fields = body.model_dump(exclude_unset=True)
    if "admin_scopes" in fields:
        _validate_admin_scopes(fields["admin_scopes"])
    try:
        record = await update_key_fields(key_id, actor_key_id=auth.key_id, **fields)
    except KeyError:
        raise OpenAIError(
            f"Unknown key id `{key_id}`.",
            status_code=404,
            error_type="invalid_request_error",
            code="key_not_found",
        ) from None
    except AdminPersistenceError as exc:
        raise OpenAIError(
            str(exc), status_code=409, error_type="api_error", code="key_store_not_file_backed"
        ) from exc
    except LastKeysWriteAdminError:
        raise OpenAIError(
            f"Refusing to remove `keys:write` admin access from `{key_id}` -- it's the last "
            "key that has it, and this would lock the deployment out of its own "
            "key-management API.",
            status_code=409,
            error_type="invalid_request_error",
            code="cannot_remove_last_admin_key",
        ) from None

    logger.info(
        "admin key_id=%s updated key_id=%s fields=%s [request_id=%s]",
        auth.key_id,
        key_id,
        sorted(fields.keys()),
        get_request_id(),
    )
    return _to_admin_key_item(record)


@router.post("/api/admin/keys/{key_id}/rotate", response_model=RotateKeyResponse)
async def rotate_key_endpoint(
    key_id: str,
    auth: AuthContext = Depends(require_scope("keys:write")),
) -> RotateKeyResponse:
    try:
        record, raw_key = await rotate_key(key_id, actor_key_id=auth.key_id)
    except KeyError:
        raise OpenAIError(
            f"Unknown key id `{key_id}`.",
            status_code=404,
            error_type="invalid_request_error",
            code="key_not_found",
        ) from None
    except AdminPersistenceError as exc:
        raise OpenAIError(
            str(exc), status_code=409, error_type="api_error", code="key_store_not_file_backed"
        ) from exc

    logger.info(
        "admin key_id=%s rotated key_id=%s [request_id=%s]",
        auth.key_id,
        key_id,
        get_request_id(),
    )
    return RotateKeyResponse(key=_to_admin_key_item(record), api_key=raw_key)


@router.delete("/api/admin/keys/{key_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_key_endpoint(
    key_id: str,
    auth: AuthContext = Depends(require_scope("keys:write")),
) -> None:
    try:
        await delete_key(key_id, actor_key_id=auth.key_id)
    except KeyError:
        raise OpenAIError(
            f"Unknown key id `{key_id}`.",
            status_code=404,
            error_type="invalid_request_error",
            code="key_not_found",
        ) from None
    except AdminPersistenceError as exc:
        raise OpenAIError(
            str(exc), status_code=409, error_type="api_error", code="key_store_not_file_backed"
        ) from exc
    except LastKeysWriteAdminError:
        raise OpenAIError(
            f"Refusing to delete `{key_id}` -- it's the last key with `keys:write` admin "
            "access, and this would lock the deployment out of its own key-management API.",
            status_code=409,
            error_type="invalid_request_error",
            code="cannot_remove_last_admin_key",
        ) from None

    logger.info(
        "admin key_id=%s deleted key_id=%s [request_id=%s]",
        auth.key_id,
        key_id,
        get_request_id(),
    )


@router.patch("/api/admin/keys/{key_id}/guardrails-configs", response_model=AdminKeyItem)
async def update_key_guardrails_configs(
    key_id: str,
    body: UpdateKeyGuardrailsConfigsRequest,
    catalog: GuardrailsCatalogService = Depends(get_guardrails_catalog_service),
    auth: AuthContext = Depends(require_scope("keys:write")),
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
            key_id, body.allowed_guardrails_configs, actor_key_id=auth.key_id
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
    auth: AuthContext = Depends(require_scope("guardrails:write")),
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
    auth: AuthContext = Depends(require_scope("guardrails:write")),
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
        values = write_editable_sections(
            config_dir, config_id, body.sections, guardrails_service, actor_key_id=auth.key_id
        )
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


def _to_prompt_injection_response(config: PromptInjectionConfig) -> PromptInjectionConfigResponse:
    return PromptInjectionConfigResponse(
        enabled=config.enabled,
        scope=config.scope.value,
        detect_evasions=config.detect_evasions,
        allow_list=list(config.allow_list),
        categories={category.value: action.value for category, action in config.categories.items()},
    )


@router.get("/api/admin/prompt-injection", response_model=PromptInjectionConfigResponse)
async def get_prompt_injection_config_endpoint(
    config: PromptInjectionConfig = Depends(get_prompt_injection_config),
    auth: AuthContext = Depends(require_scope("prompt_injection:write")),
) -> PromptInjectionConfigResponse:
    return _to_prompt_injection_response(config)


@router.patch("/api/admin/prompt-injection", response_model=PromptInjectionConfigResponse)
async def update_prompt_injection_config_endpoint(
    body: UpdatePromptInjectionConfigRequest,
    auth: AuthContext = Depends(require_scope("prompt_injection:write")),
) -> PromptInjectionConfigResponse:
    fields = body.model_dump(exclude_unset=True)

    scope: InjectionScope | None = None
    if "scope" in fields:
        try:
            scope = InjectionScope(fields["scope"])
        except ValueError:
            raise OpenAIError(
                f"Invalid scope `{fields['scope']}`.",
                status_code=422,
                error_type="invalid_request_error",
                param="scope",
                code="invalid_scope",
            ) from None

    categories: dict[InjectionCategory, InjectionAction] | None = None
    if "categories" in fields:
        categories = {}
        for raw_category, raw_action in fields["categories"].items():
            try:
                category = InjectionCategory(raw_category)
            except ValueError:
                raise OpenAIError(
                    f"Unknown prompt-injection category `{raw_category}`.",
                    status_code=422,
                    error_type="invalid_request_error",
                    param="categories",
                    code="invalid_category",
                ) from None
            try:
                action = InjectionAction(raw_action)
            except ValueError:
                raise OpenAIError(
                    f"Invalid prompt-injection action `{raw_action}`.",
                    status_code=422,
                    error_type="invalid_request_error",
                    param="categories",
                    code="invalid_action",
                ) from None
            categories[category] = action

    updated = await update_prompt_injection_config(
        actor_key_id=auth.key_id,
        enabled=fields.get("enabled"),
        scope=scope,
        detect_evasions=fields.get("detect_evasions"),
        allow_list=fields.get("allow_list"),
        categories=categories,
    )

    logger.info(
        "admin key_id=%s updated prompt-injection config fields=%s [request_id=%s]",
        auth.key_id,
        sorted(fields.keys()),
        get_request_id(),
    )
    return _to_prompt_injection_response(updated)


@router.post("/api/admin/prompt-injection/test", response_model=PromptInjectionTestResponse)
async def test_prompt_injection_config(
    body: PromptInjectionTestRequest,
    config: PromptInjectionConfig = Depends(get_prompt_injection_config),
    auth: AuthContext = Depends(require_scope("prompt_injection:write")),
) -> PromptInjectionTestResponse:
    # Runs against the current *saved* config regardless of `enabled` --
    # deliberately works pre-enable so an admin can test a policy before
    # switching it on, matching OpenRouter's own "test in flag mode first"
    # guidance. Read-only: never persists anything.
    matches = scan_message(body.text, config)
    action = resolve_overall_action(matches, config)
    redacted_preview = redact_preview(body.text, matches) if action is InjectionAction.REDACT else None
    return PromptInjectionTestResponse(
        action=action.value,
        matches=[
            PromptInjectionTestMatch(
                category=m.category.value,
                pattern_name=m.pattern_name,
                matched_text=m.matched_text,
                via=m.via,
            )
            for m in matches
        ],
        redacted_preview=redacted_preview,
    )


def _to_output_leak_response(config: OutputLeakConfig) -> OutputLeakConfigResponse:
    return OutputLeakConfigResponse(
        enabled=config.enabled,
        allow_list=list(config.allow_list),
        categories={category.value: action.value for category, action in config.categories.items()},
        custom_patterns=[
            OutputLeakCustomPatternItem(name=p.name, pattern=p.pattern, action=p.action.value)
            for p in config.custom_patterns
        ],
    )


@router.get("/api/admin/output-leak", response_model=OutputLeakConfigResponse)
async def get_output_leak_config_endpoint(
    config: OutputLeakConfig = Depends(get_output_leak_config),
    auth: AuthContext = Depends(require_scope("output_leak:write")),
) -> OutputLeakConfigResponse:
    return _to_output_leak_response(config)


@router.patch("/api/admin/output-leak", response_model=OutputLeakConfigResponse)
async def update_output_leak_config_endpoint(
    body: UpdateOutputLeakConfigRequest,
    auth: AuthContext = Depends(require_scope("output_leak:write")),
) -> OutputLeakConfigResponse:
    fields = body.model_dump(exclude_unset=True)

    categories: dict[OutputLeakCategory, OutputLeakAction] | None = None
    if "categories" in fields:
        categories = {}
        for raw_category, raw_action in fields["categories"].items():
            try:
                category = OutputLeakCategory(raw_category)
            except ValueError:
                raise OpenAIError(
                    f"Unknown output-leak category `{raw_category}`.",
                    status_code=422,
                    error_type="invalid_request_error",
                    param="categories",
                    code="invalid_category",
                ) from None
            if category is OutputLeakCategory.CUSTOM:
                raise OpenAIError(
                    "`custom` is not an independently configurable category -- use "
                    "custom_patterns instead.",
                    status_code=422,
                    error_type="invalid_request_error",
                    param="categories",
                    code="invalid_category",
                )
            try:
                action = OutputLeakAction(raw_action)
            except ValueError:
                raise OpenAIError(
                    f"Invalid output-leak action `{raw_action}`.",
                    status_code=422,
                    error_type="invalid_request_error",
                    param="categories",
                    code="invalid_action",
                ) from None
            categories[category] = action

    custom_patterns: list[CustomPattern] | None = None
    if "custom_patterns" in fields:
        try:
            custom_patterns = [
                CustomPattern(name=p["name"], pattern=p["pattern"], action=p["action"])
                for p in fields["custom_patterns"]
            ]
        except (ValueError, ValidationError) as exc:
            raise OpenAIError(
                f"Invalid custom_patterns entry: {exc}",
                status_code=422,
                error_type="invalid_request_error",
                param="custom_patterns",
                code="invalid_custom_pattern",
            ) from exc

    updated = await update_output_leak_config(
        actor_key_id=auth.key_id,
        enabled=fields.get("enabled"),
        allow_list=fields.get("allow_list"),
        categories=categories,
        custom_patterns=custom_patterns,
    )

    logger.info(
        "admin key_id=%s updated output-leak config fields=%s [request_id=%s]",
        auth.key_id,
        sorted(fields.keys()),
        get_request_id(),
    )
    return _to_output_leak_response(updated)


@router.post("/api/admin/output-leak/test", response_model=OutputLeakTestResponse)
async def test_output_leak_config(
    body: OutputLeakTestRequest,
    config: OutputLeakConfig = Depends(get_output_leak_config),
    auth: AuthContext = Depends(require_scope("output_leak:write")),
) -> OutputLeakTestResponse:
    # Runs against the current *saved* config regardless of `enabled` --
    # same "test before you turn it on" posture as
    # test_prompt_injection_config above. Read-only: never persists
    # anything.
    matches = scan_output_leak_text(body.text, config)
    action = resolve_output_leak_action(matches)
    redacted_preview = redact_output_leak_text(body.text, matches) if action is OutputLeakAction.REDACT else None
    return OutputLeakTestResponse(
        action=action.value,
        matches=[
            OutputLeakTestMatch(
                category=m.category.value,
                pattern_name=m.pattern_name,
                matched_text=m.matched_text,
                action=m.action.value,
            )
            for m in matches
        ],
        redacted_preview=redacted_preview,
    )


def _to_guardrail_event_item(event) -> GuardrailEventItem:
    return GuardrailEventItem(
        id=event.id,
        timestamp=event.timestamp,
        request_id=event.request_id,
        key_id=event.key_id,
        guardrail=event.guardrail,
        model=event.model,
        category=event.category,
        pattern_name=event.pattern_name,
        action=event.action,
        via=event.via,
        snippet=event.snippet,
    )


@router.get("/api/admin/guardrail-events", response_model=GuardrailEventsResponse)
async def list_guardrail_events_endpoint(
    limit: int = Query(50, ge=1, le=200),
    key_id: str | None = None,
    guardrail: str | None = Query(None, pattern="^(prompt_injection|output_leak)$"),
    action: str | None = Query(None, pattern="^(flag|redact|block)$"),
    auth: AuthContext = Depends(require_scope("activity:read")),
) -> GuardrailEventsResponse:
    # Gated on activity:read (not a new dedicated scope, and not
    # prompt_injection:write/output_leak:write) -- this is the per-request
    # observability counterpart to GET /api/admin/activity/overview
    # (same scope, same "surfaced in the Activity dashboard" audience),
    # not a config-write capability like the other two guardrail scopes.
    return GuardrailEventsResponse(
        events=[
            _to_guardrail_event_item(e)
            for e in list_guardrail_events(limit=limit, key_id=key_id, guardrail=guardrail, action=action)
        ]
    )


def _to_audit_entry_item(entry) -> AdminAuditEntryItem:
    return AdminAuditEntryItem(
        id=entry.id,
        timestamp=entry.timestamp,
        actor_key_id=entry.actor_key_id,
        resource_type=entry.resource_type,
        resource_id=entry.resource_id,
        action=entry.action,
        summary=entry.summary,
    )


@router.get("/api/admin/audit-log", response_model=AdminAuditLogResponse)
async def list_audit_log(
    limit: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(require_full_admin),
) -> AdminAuditLogResponse:
    return AdminAuditLogResponse(
        entries=[_to_audit_entry_item(e) for e in list_audit_entries(limit=limit)]
    )


@router.post("/api/admin/audit-log/{entry_id}/revert", response_model=RevertAuditEntryResponse)
async def revert_audit_log_entry(
    entry_id: str,
    guardrails_service: GuardrailsService = Depends(get_guardrails_service),
    auth: AuthContext = Depends(require_full_admin),
) -> RevertAuditEntryResponse:
    try:
        original, revert = revert_audit_entry_by_id(entry_id, actor_key_id=auth.key_id)
    except KeyError:
        raise OpenAIError(
            f"Unknown audit log entry `{entry_id}`.",
            status_code=404,
            error_type="invalid_request_error",
            code="audit_entry_not_found",
        ) from None

    # Invalidate whichever in-process cache actually holds this resource's
    # state -- same as every write endpoint above already does for its own
    # resource, since a revert is just another write to the same file.
    if original.resource_type == "api_keys":
        get_key_store.cache_clear()
    elif original.resource_type == "prompt_injection":
        get_prompt_injection_config.cache_clear()
    elif original.resource_type == "output_leak":
        get_output_leak_config.cache_clear()
    elif original.resource_type == "guardrails_config" and original.resource_id:
        guardrails_service.invalidate(original.resource_id)

    logger.info(
        "admin key_id=%s reverted audit entry_id=%s resource_type=%s resource_id=%s "
        "[request_id=%s]",
        auth.key_id,
        entry_id,
        original.resource_type,
        original.resource_id,
        get_request_id(),
    )
    return RevertAuditEntryResponse(
        reverted_entry=_to_audit_entry_item(original),
        revert_entry=_to_audit_entry_item(revert),
    )
