"use strict";

// API_KEY_STORAGE_KEY, escapeHtml, setStatusText, formatError come from common.js.

const apiKeyInput = document.getElementById("api-key");
const adminStatusEl = document.getElementById("admin-status");
const keysSection = document.getElementById("keys-section");
const keysTableEl = document.getElementById("keys-table");
const guardrailsSection = document.getElementById("guardrails-section");
const guardrailsCardsEl = document.getElementById("guardrails-cards");
const promptInjectionSection = document.getElementById("prompt-injection-section");
const outputLeakSection = document.getElementById("output-leak-section");
const auditLogSection = document.getElementById("audit-log-section");
const auditLogTableEl = document.getElementById("audit-log-table");
const piEnabledInput = document.getElementById("pi-enabled");
const piScopeSelect = document.getElementById("pi-scope");
const piDetectEvasionsInput = document.getElementById("pi-detect-evasions");
const piCategoriesEl = document.getElementById("pi-categories");
const piAllowListInput = document.getElementById("pi-allow-list");
const piSaveButton = document.getElementById("pi-save");
const piStatusEl = document.getElementById("pi-status");
const piTestInput = document.getElementById("pi-test-input");
const piTestRunButton = document.getElementById("pi-test-run");
const piTestResultEl = document.getElementById("pi-test-result");
const olEnabledInput = document.getElementById("ol-enabled");
const olCategoriesEl = document.getElementById("ol-categories");
const olAllowListInput = document.getElementById("ol-allow-list");
const olCustomPatternsEl = document.getElementById("ol-custom-patterns");
const olAddCustomPatternButton = document.getElementById("ol-add-custom-pattern");
const olSaveButton = document.getElementById("ol-save");
const olStatusEl = document.getElementById("ol-status");
const olTestInput = document.getElementById("ol-test-input");
const olTestRunButton = document.getElementById("ol-test-run");
const olTestResultEl = document.getElementById("ol-test-result");
const createKeyForm = document.getElementById("create-key-form");
const createKeyIdInput = document.getElementById("create-key-id");
const createKeyModelsInput = document.getElementById("create-key-models");
const createKeyRpmInput = document.getElementById("create-key-rpm");
const createKeyBudgetDailyInput = document.getElementById("create-key-budget-daily");
const createKeyBudgetMonthlyInput = document.getElementById("create-key-budget-monthly");
const createKeyAdminInput = document.getElementById("create-key-admin");
const createKeyScopesEl = document.getElementById("create-key-scopes");
const createKeyStatusEl = document.getElementById("create-key-status");
const createKeyResultEl = document.getElementById("create-key-result");

// Mirrors app.auth.keys.ALL_ADMIN_SCOPES -- kept as a small fixed list here
// rather than fetched from the API, same as this file already hardcodes
// nothing else server-derived except knownConfigIds (which genuinely is
// operator-defined and can't be a constant).
const ALL_ADMIN_SCOPES = [
  "keys:write",
  "guardrails:write",
  "prompt_injection:write",
  "output_leak:write",
  "metrics:read",
  "activity:read",
];

function renderScopeCheckboxes(container, checkedScopes) {
  container.innerHTML = "";
  const checkboxes = {};
  for (const scope of ALL_ADMIN_SCOPES) {
    const label = document.createElement("label");
    label.className = "checkbox-field admin-checkbox";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = checkedScopes.includes(scope);
    checkboxes[scope] = checkbox;
    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(scope));
    container.appendChild(label);
  }
  return checkboxes;
}

let createKeyScopeCheckboxes = renderScopeCheckboxes(createKeyScopesEl, []);

function splitCommaList(value) {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

function renderRawKeyCallout(container, apiKey) {
  container.innerHTML = "";
  const box = document.createElement("div");
  box.className = "admin-raw-key";
  const label = document.createElement("span");
  label.textContent = "Raw key (shown once -- copy it now, it can't be retrieved again):";
  const code = document.createElement("code");
  code.textContent = apiKey;
  box.appendChild(label);
  box.appendChild(code);
  container.appendChild(box);
}

let knownConfigIds = [];

function currentApiKey() {
  return apiKeyInput.value.trim();
}

async function apiFetch(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Authorization: `Bearer ${currentApiKey()}`,
      ...(options.headers || {}),
    },
  });
  let body = null;
  try {
    body = await response.json();
  } catch (err) {
    // No JSON body (e.g. a network-level failure) -- callers check response.ok.
  }
  return { response, body };
}

// Blank means "unlimited" for a token-budget field -- unlike
// requests_per_minute (which always has some positive value), None/null
// is a real, meaningful state here (see APIKeyRecord.token_budget_daily/
// _monthly), so an empty box sends an explicit `null`, not an omitted
// field.
function parseOptionalBudget(input) {
  const trimmed = input.value.trim();
  if (trimmed === "") return null;
  const parsed = parseInt(trimmed, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function renderKeysTable(keys) {
  keysTableEl.innerHTML = "";
  const table = document.createElement("table");
  table.className = "admin-table";

  const thead = document.createElement("thead");
  thead.innerHTML =
    "<tr><th>Key</th><th>Admin</th><th>Admin scopes</th><th>Allowed models</th><th>Requests/min</th>" +
    "<th>Daily budget</th><th>Monthly budget</th>" +
    "<th></th><th>Allowed guardrails configs</th><th></th><th>Actions</th></tr>";
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const key of keys) {
    const tr = document.createElement("tr");

    const idTd = document.createElement("td");
    idTd.textContent = key.id;
    tr.appendChild(idTd);

    const adminTd = document.createElement("td");
    const adminCheckbox = document.createElement("input");
    adminCheckbox.type = "checkbox";
    adminCheckbox.checked = key.is_admin;
    adminTd.appendChild(adminCheckbox);
    tr.appendChild(adminTd);

    const scopesTd = document.createElement("td");
    const scopesWrapper = document.createElement("div");
    scopesWrapper.className = "admin-config-checkboxes";
    scopesTd.appendChild(scopesWrapper);
    const scopeCheckboxes = renderScopeCheckboxes(scopesWrapper, key.admin_scopes);
    tr.appendChild(scopesTd);

    const modelsTd = document.createElement("td");
    const modelsInput = document.createElement("input");
    modelsInput.type = "text";
    modelsInput.value = key.allowed_models.join(", ");
    modelsTd.appendChild(modelsInput);
    tr.appendChild(modelsTd);

    const rpmTd = document.createElement("td");
    const rpmInput = document.createElement("input");
    rpmInput.type = "number";
    rpmInput.min = "1";
    rpmInput.value = key.requests_per_minute;
    rpmTd.appendChild(rpmInput);
    tr.appendChild(rpmTd);

    const budgetDailyTd = document.createElement("td");
    const budgetDailyInput = document.createElement("input");
    budgetDailyInput.type = "number";
    budgetDailyInput.min = "1";
    budgetDailyInput.placeholder = "unlimited";
    if (key.token_budget_daily !== null) budgetDailyInput.value = key.token_budget_daily;
    budgetDailyTd.appendChild(budgetDailyInput);
    tr.appendChild(budgetDailyTd);

    const budgetMonthlyTd = document.createElement("td");
    const budgetMonthlyInput = document.createElement("input");
    budgetMonthlyInput.type = "number";
    budgetMonthlyInput.min = "1";
    budgetMonthlyInput.placeholder = "unlimited";
    if (key.token_budget_monthly !== null) budgetMonthlyInput.value = key.token_budget_monthly;
    budgetMonthlyTd.appendChild(budgetMonthlyInput);
    tr.appendChild(budgetMonthlyTd);

    const keyActionTd = document.createElement("td");
    const keySaveButton = document.createElement("button");
    keySaveButton.type = "button";
    keySaveButton.textContent = "Save";
    const keyRowStatus = document.createElement("span");
    keyRowStatus.className = "admin-row-status";
    keySaveButton.addEventListener("click", async () => {
      const allowedModels = splitCommaList(modelsInput.value);
      if (allowedModels.length === 0) {
        setStatusText(keyRowStatus, "Allowed models can't be empty.", true);
        return;
      }
      const rpm = parseInt(rpmInput.value, 10);
      const adminScopes = ALL_ADMIN_SCOPES.filter((scope) => scopeCheckboxes[scope].checked);
      keySaveButton.disabled = true;
      setStatusText(keyRowStatus, "Saving...");
      const { response, body } = await apiFetch(`/api/admin/keys/${encodeURIComponent(key.id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          allowed_models: allowedModels,
          requests_per_minute: Number.isFinite(rpm) ? rpm : undefined,
          is_admin: adminCheckbox.checked,
          admin_scopes: adminScopes,
          token_budget_daily: parseOptionalBudget(budgetDailyInput),
          token_budget_monthly: parseOptionalBudget(budgetMonthlyInput),
        }),
      });
      keySaveButton.disabled = false;
      setStatusText(keyRowStatus, response.ok ? "Saved." : formatError(body), !response.ok);
    });
    keyActionTd.appendChild(keySaveButton);
    keyActionTd.appendChild(keyRowStatus);
    tr.appendChild(keyActionTd);

    const configsTd = document.createElement("td");
    // The flex layout goes on an inner wrapper, not the <td> itself --
    // overriding a table cell's own `display` away from `table-cell`
    // breaks its participation in the table's row-height/border-collapse
    // layout (visibly misaligned row borders in some browsers).
    const configsWrapper = document.createElement("div");
    configsWrapper.className = "admin-config-checkboxes";
    configsTd.appendChild(configsWrapper);
    // null means unrestricted (this key can set guardrails.config_id to
    // anything, the state every key has until an admin explicitly
    // restricts it) -- distinct from an empty array, which means
    // deliberately restricted to nothing.
    const isUnrestricted = key.allowed_guardrails_configs === null;
    if (isUnrestricted) {
      const note = document.createElement("p");
      note.className = "admin-hint";
      note.textContent = "Unrestricted (any config_id allowed). Check any box to restrict.";
      configsWrapper.appendChild(note);
    }
    const checkboxes = {};
    for (const configId of knownConfigIds) {
      const label = document.createElement("label");
      label.className = "checkbox-field admin-checkbox";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = !isUnrestricted && key.allowed_guardrails_configs.includes(configId);
      checkboxes[configId] = checkbox;
      label.appendChild(checkbox);
      label.appendChild(document.createTextNode(configId));
      configsWrapper.appendChild(label);
    }
    tr.appendChild(configsTd);

    const actionTd = document.createElement("td");
    const saveButton = document.createElement("button");
    saveButton.type = "button";
    saveButton.textContent = "Save";
    const rowStatus = document.createElement("span");
    rowStatus.className = "admin-row-status";
    saveButton.addEventListener("click", async () => {
      const allowed = knownConfigIds.filter((id) => checkboxes[id].checked);
      saveButton.disabled = true;
      setStatusText(rowStatus, "Saving...");
      const { response, body } = await apiFetch(
        `/api/admin/keys/${encodeURIComponent(key.id)}/guardrails-configs`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ allowed_guardrails_configs: allowed }),
        },
      );
      saveButton.disabled = false;
      setStatusText(rowStatus, response.ok ? "Saved." : formatError(body), !response.ok);
    });
    actionTd.appendChild(saveButton);
    actionTd.appendChild(rowStatus);
    tr.appendChild(actionTd);

    const lifecycleTd = document.createElement("td");
    // Same reasoning as configsWrapper above: the flex layout goes on an
    // inner wrapper, never directly on the <td>.
    const lifecycleWrapper = document.createElement("div");
    lifecycleWrapper.className = "admin-table-actions";
    lifecycleTd.appendChild(lifecycleWrapper);

    const rotateButton = document.createElement("button");
    rotateButton.type = "button";
    rotateButton.textContent = "Rotate";
    const lifecycleStatus = document.createElement("span");
    lifecycleStatus.className = "admin-row-status";
    rotateButton.addEventListener("click", async () => {
      if (
        !window.confirm(
          `Rotate "${key.id}"? Its current raw key will stop working immediately.`,
        )
      ) {
        return;
      }
      rotateButton.disabled = true;
      setStatusText(lifecycleStatus, "Rotating...");
      const { response, body } = await apiFetch(
        `/api/admin/keys/${encodeURIComponent(key.id)}/rotate`,
        { method: "POST" },
      );
      rotateButton.disabled = false;
      if (response.ok) {
        setStatusText(lifecycleStatus, "Rotated.");
        renderRawKeyCallout(createKeyResultEl, body.api_key);
      } else {
        setStatusText(lifecycleStatus, formatError(body), true);
      }
    });

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "admin-danger";
    deleteButton.textContent = "Delete";
    deleteButton.addEventListener("click", async () => {
      if (!window.confirm(`Delete key "${key.id}"? This can't be undone.`)) {
        return;
      }
      deleteButton.disabled = true;
      setStatusText(lifecycleStatus, "Deleting...");
      const { response, body } = await apiFetch(`/api/admin/keys/${encodeURIComponent(key.id)}`, {
        method: "DELETE",
      });
      if (response.ok || response.status === 204) {
        tr.remove();
      } else {
        deleteButton.disabled = false;
        setStatusText(lifecycleStatus, formatError(body), true);
      }
    });

    lifecycleWrapper.appendChild(rotateButton);
    lifecycleWrapper.appendChild(deleteButton);
    lifecycleWrapper.appendChild(lifecycleStatus);
    tr.appendChild(lifecycleTd);

    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  const scrollWrapper = document.createElement("div");
  scrollWrapper.className = "admin-table-scroll";
  scrollWrapper.appendChild(table);
  keysTableEl.appendChild(scrollWrapper);
}

function renderAuditLogTable(entries) {
  auditLogTableEl.innerHTML = "";
  const table = document.createElement("table");
  table.className = "admin-table";

  const thead = document.createElement("thead");
  thead.innerHTML =
    "<tr><th>Time</th><th>Actor</th><th>Resource</th><th>Action</th><th>Summary</th><th></th></tr>";
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const entry of entries) {
    const tr = document.createElement("tr");

    const timeTd = document.createElement("td");
    timeTd.textContent = entry.timestamp;
    tr.appendChild(timeTd);

    const actorTd = document.createElement("td");
    actorTd.textContent = entry.actor_key_id;
    tr.appendChild(actorTd);

    const resourceTd = document.createElement("td");
    resourceTd.textContent = entry.resource_id
      ? `${entry.resource_type}: ${entry.resource_id}`
      : entry.resource_type;
    tr.appendChild(resourceTd);

    const actionTd = document.createElement("td");
    actionTd.textContent = entry.action;
    tr.appendChild(actionTd);

    const summaryTd = document.createElement("td");
    summaryTd.textContent = entry.summary;
    tr.appendChild(summaryTd);

    const revertTd = document.createElement("td");
    const revertButton = document.createElement("button");
    revertButton.type = "button";
    revertButton.textContent = "Revert";
    const revertStatus = document.createElement("span");
    revertStatus.className = "admin-row-status";
    revertButton.addEventListener("click", async () => {
      if (
        !window.confirm(
          `Revert "${entry.action}" (${entry.summary})? This restores the file content from ` +
            "just before that change and is itself recorded as a new entry.",
        )
      ) {
        return;
      }
      revertButton.disabled = true;
      setStatusText(revertStatus, "Reverting...");
      const { response, body } = await apiFetch(`/api/admin/audit-log/${entry.id}/revert`, {
        method: "POST",
      });
      if (response.ok) {
        // Re-fetches everything the panel currently shows (including the
        // audit log itself, since isFullAdmin is true here) -- a revert
        // can change key/guardrails/prompt-injection state too, not just
        // the log.
        await loadAdminPanel(currentAdminScopes, currentIsFullAdmin);
      } else {
        revertButton.disabled = false;
        setStatusText(revertStatus, formatError(body), true);
      }
    });
    revertTd.appendChild(revertButton);
    revertTd.appendChild(revertStatus);
    tr.appendChild(revertTd);

    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  const scrollWrapper = document.createElement("div");
  scrollWrapper.className = "admin-table-scroll";
  scrollWrapper.appendChild(table);
  auditLogTableEl.appendChild(scrollWrapper);
}

async function loadAuditLog() {
  const { response, body } = await apiFetch("/api/admin/audit-log");
  if (!response.ok) return;
  renderAuditLogTable(body.entries);
  auditLogSection.hidden = false;
}

function renderGuardrailsCards(configs) {
  guardrailsCardsEl.innerHTML = "";
  for (const config of configs) {
    const card = document.createElement("div");
    card.className = "admin-card";

    const heading = document.createElement("h3");
    heading.textContent = config.config_id;
    card.appendChild(heading);

    if (!config.editable) {
      const note = document.createElement("p");
      note.className = "admin-hint";
      note.textContent = "Not structurally editable from this UI -- edit config.yml by hand.";
      card.appendChild(note);
      guardrailsCardsEl.appendChild(card);
      continue;
    }

    if (config.error) {
      const note = document.createElement("p");
      note.className = "admin-hint error";
      note.textContent = `Could not read current values: ${config.error}`;
      card.appendChild(note);
    }

    const textareas = {};
    for (const section of config.sections) {
      const label = document.createElement("label");
      label.className = "field";
      const span = document.createElement("span");
      span.textContent = `${section.label} (one per line)`;
      label.appendChild(span);
      const textarea = document.createElement("textarea");
      textarea.rows = Math.max(3, section.items.length);
      textarea.value = section.items.join("\n");
      textareas[section.field] = textarea;
      label.appendChild(textarea);
      card.appendChild(label);
    }

    const saveButton = document.createElement("button");
    saveButton.type = "button";
    saveButton.textContent = "Save";
    const cardStatus = document.createElement("span");
    cardStatus.className = "admin-row-status";
    saveButton.addEventListener("click", async () => {
      const sections = {};
      for (const [field, textarea] of Object.entries(textareas)) {
        sections[field] = textarea.value
          .split("\n")
          .map((line) => line.trim())
          .filter((line) => line.length > 0);
      }
      saveButton.disabled = true;
      setStatusText(cardStatus, "Saving...");
      const { response, body } = await apiFetch(
        `/api/admin/guardrails/configs/${encodeURIComponent(config.config_id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sections }),
        },
      );
      saveButton.disabled = false;
      if (response.ok) {
        setStatusText(cardStatus, "Saved.");
        for (const section of body.sections) {
          if (textareas[section.field]) {
            textareas[section.field].value = section.items.join("\n");
          }
        }
      } else {
        setStatusText(cardStatus, formatError(body), true);
      }
    });
    card.appendChild(saveButton);
    card.appendChild(cardStatus);

    guardrailsCardsEl.appendChild(card);
  }
}

// "instruction_override" -> "Instruction override".
function categoryLabel(value) {
  const words = value.split("_");
  return words[0].charAt(0).toUpperCase() + words[0].slice(1) + " " + words.slice(1).join(" ");
}

function renderPromptInjectionCard(config) {
  piEnabledInput.checked = config.enabled;
  piScopeSelect.value = config.scope;
  piDetectEvasionsInput.checked = config.detect_evasions;
  piAllowListInput.value = config.allow_list.join("\n");

  piCategoriesEl.innerHTML = "";
  const table = document.createElement("table");
  table.className = "admin-table";
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>Category</th><th>Action</th></tr>";
  table.appendChild(thead);
  const tbody = document.createElement("tbody");

  const categorySelects = {};
  for (const [category, action] of Object.entries(config.categories)) {
    const tr = document.createElement("tr");
    const nameTd = document.createElement("td");
    nameTd.textContent = categoryLabel(category);
    tr.appendChild(nameTd);

    const actionTd = document.createElement("td");
    const select = document.createElement("select");
    for (const value of ["disabled", "flag", "redact", "block"]) {
      select.appendChild(new Option(value, value, false, value === action));
    }
    categorySelects[category] = select;
    actionTd.appendChild(select);
    tr.appendChild(actionTd);

    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  piCategoriesEl.appendChild(table);

  piSaveButton.onclick = async () => {
    const categories = {};
    for (const [category, select] of Object.entries(categorySelects)) {
      categories[category] = select.value;
    }
    const allowList = piAllowListInput.value
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.length > 0);

    piSaveButton.disabled = true;
    setStatusText(piStatusEl, "Saving...");
    const { response, body } = await apiFetch("/api/admin/prompt-injection", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: piEnabledInput.checked,
        scope: piScopeSelect.value,
        detect_evasions: piDetectEvasionsInput.checked,
        allow_list: allowList,
        categories,
      }),
    });
    piSaveButton.disabled = false;
    if (response.ok) {
      setStatusText(piStatusEl, "Saved.");
      renderPromptInjectionCard(body);
    } else {
      setStatusText(piStatusEl, formatError(body), true);
    }
  };
}

function renderPromptInjectionTestResult(result) {
  piTestResultEl.innerHTML = "";

  const actionEl = document.createElement("p");
  actionEl.innerHTML = `Action: <strong>${escapeHtml(result.action)}</strong>`;
  piTestResultEl.appendChild(actionEl);

  if (result.matches.length === 0) {
    const none = document.createElement("p");
    none.className = "admin-hint";
    none.textContent = "No matches.";
    piTestResultEl.appendChild(none);
  } else {
    const list = document.createElement("ul");
    for (const match of result.matches) {
      const li = document.createElement("li");
      li.textContent = `${categoryLabel(match.category)} (${match.pattern_name}, via ${match.via}): "${match.matched_text}"`;
      list.appendChild(li);
    }
    piTestResultEl.appendChild(list);
  }

  if (result.redacted_preview !== null && result.redacted_preview !== undefined) {
    const previewLabel = document.createElement("p");
    previewLabel.textContent = "Redacted preview:";
    const preview = document.createElement("pre");
    preview.textContent = result.redacted_preview;
    piTestResultEl.appendChild(previewLabel);
    piTestResultEl.appendChild(preview);
  }
}

piTestRunButton.addEventListener("click", async () => {
  const text = piTestInput.value.trim();
  if (!text) {
    return;
  }
  piTestRunButton.disabled = true;
  piTestResultEl.innerHTML = "";
  const { response, body } = await apiFetch("/api/admin/prompt-injection/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  piTestRunButton.disabled = false;
  if (response.ok) {
    renderPromptInjectionTestResult(body);
  } else {
    piTestResultEl.textContent = formatError(body);
  }
});

function addCustomPatternRow(pattern) {
  const row = document.createElement("div");
  row.className = "row ol-custom-pattern-row";

  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.placeholder = "name";
  nameInput.value = pattern ? pattern.name : "";

  const patternInput = document.createElement("input");
  patternInput.type = "text";
  patternInput.placeholder = "regex pattern";
  patternInput.value = pattern ? pattern.pattern : "";

  const actionSelect = document.createElement("select");
  for (const value of ["disabled", "flag", "redact", "block"]) {
    actionSelect.appendChild(
      new Option(value, value, false, pattern ? value === pattern.action : value === "flag"),
    );
  }

  const removeButton = document.createElement("button");
  removeButton.type = "button";
  removeButton.textContent = "Remove";
  removeButton.onclick = () => row.remove();

  row.appendChild(nameInput);
  row.appendChild(patternInput);
  row.appendChild(actionSelect);
  row.appendChild(removeButton);
  olCustomPatternsEl.appendChild(row);
}

function readCustomPatternsFromDom() {
  const patterns = [];
  for (const row of olCustomPatternsEl.querySelectorAll(".ol-custom-pattern-row")) {
    const [nameInput, patternInput, actionSelect] = row.querySelectorAll("input, select");
    const name = nameInput.value.trim();
    const pattern = patternInput.value.trim();
    if (!name || !pattern) continue;
    patterns.push({ name, pattern, action: actionSelect.value });
  }
  return patterns;
}

olAddCustomPatternButton.addEventListener("click", () => addCustomPatternRow(null));

function renderOutputLeakCard(config) {
  olEnabledInput.checked = config.enabled;
  olAllowListInput.value = config.allow_list.join("\n");

  olCategoriesEl.innerHTML = "";
  const table = document.createElement("table");
  table.className = "admin-table";
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>Category</th><th>Action</th></tr>";
  table.appendChild(thead);
  const tbody = document.createElement("tbody");

  const categorySelects = {};
  for (const [category, action] of Object.entries(config.categories)) {
    const tr = document.createElement("tr");
    const nameTd = document.createElement("td");
    nameTd.textContent = categoryLabel(category);
    tr.appendChild(nameTd);

    const actionTd = document.createElement("td");
    const select = document.createElement("select");
    for (const value of ["disabled", "flag", "redact", "block"]) {
      select.appendChild(new Option(value, value, false, value === action));
    }
    categorySelects[category] = select;
    actionTd.appendChild(select);
    tr.appendChild(actionTd);

    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  olCategoriesEl.appendChild(table);

  olCustomPatternsEl.innerHTML = "";
  for (const pattern of config.custom_patterns) {
    addCustomPatternRow(pattern);
  }

  olSaveButton.onclick = async () => {
    const categories = {};
    for (const [category, select] of Object.entries(categorySelects)) {
      categories[category] = select.value;
    }
    const allowList = olAllowListInput.value
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.length > 0);
    const customPatterns = readCustomPatternsFromDom();

    olSaveButton.disabled = true;
    setStatusText(olStatusEl, "Saving...");
    const { response, body } = await apiFetch("/api/admin/output-leak", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: olEnabledInput.checked,
        allow_list: allowList,
        categories,
        custom_patterns: customPatterns,
      }),
    });
    olSaveButton.disabled = false;
    if (response.ok) {
      setStatusText(olStatusEl, "Saved.");
      renderOutputLeakCard(body);
    } else {
      setStatusText(olStatusEl, formatError(body), true);
    }
  };
}

function renderOutputLeakTestResult(result) {
  olTestResultEl.innerHTML = "";

  const actionEl = document.createElement("p");
  actionEl.innerHTML = `Action: <strong>${escapeHtml(result.action)}</strong>`;
  olTestResultEl.appendChild(actionEl);

  if (result.matches.length === 0) {
    const none = document.createElement("p");
    none.className = "admin-hint";
    none.textContent = "No matches.";
    olTestResultEl.appendChild(none);
  } else {
    const list = document.createElement("ul");
    for (const match of result.matches) {
      const li = document.createElement("li");
      li.textContent = `${categoryLabel(match.category)} (${match.pattern_name}, action ${match.action}): "${match.matched_text}"`;
      list.appendChild(li);
    }
    olTestResultEl.appendChild(list);
  }

  if (result.redacted_preview !== null && result.redacted_preview !== undefined) {
    const previewLabel = document.createElement("p");
    previewLabel.textContent = "Redacted preview:";
    const preview = document.createElement("pre");
    preview.textContent = result.redacted_preview;
    olTestResultEl.appendChild(previewLabel);
    olTestResultEl.appendChild(preview);
  }
}

olTestRunButton.addEventListener("click", async () => {
  const text = olTestInput.value.trim();
  if (!text) {
    return;
  }
  olTestRunButton.disabled = true;
  olTestResultEl.innerHTML = "";
  const { response, body } = await apiFetch("/api/admin/output-leak/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  olTestRunButton.disabled = false;
  if (response.ok) {
    renderOutputLeakTestResult(body);
  } else {
    olTestResultEl.textContent = formatError(body);
  }
});

// Tracked so a Revert click (deep inside renderAuditLogTable) can refresh
// the rest of the panel afterward without re-deriving these from another
// whoami round-trip.
let currentAdminScopes = [];
let currentIsFullAdmin = false;

async function loadAdminPanel(adminScopes, isFullAdmin) {
  currentAdminScopes = adminScopes;
  currentIsFullAdmin = isFullAdmin;

  // Only fetches/shows the sections this key's admin_scopes actually cover
  // -- a key scoped to e.g. just "guardrails:write" gets a working
  // guardrails editor with no keys table, not a page full of 403s. See
  // app.auth.keys.ALL_ADMIN_SCOPES / the "Admin API" README section.
  const tasks = [];

  if (adminScopes.includes("guardrails:write")) {
    tasks.push(
      apiFetch("/api/admin/guardrails/configs").then(({ response, body }) => {
        if (!response.ok) return;
        knownConfigIds = body.configs.map((c) => c.config_id);
        renderGuardrailsCards(body.configs);
        guardrailsSection.hidden = false;
      }),
    );
  }

  if (adminScopes.includes("prompt_injection:write")) {
    tasks.push(
      apiFetch("/api/admin/prompt-injection").then(({ response, body }) => {
        if (!response.ok) return;
        renderPromptInjectionCard(body);
        promptInjectionSection.hidden = false;
      }),
    );
  }

  if (adminScopes.includes("output_leak:write")) {
    tasks.push(
      apiFetch("/api/admin/output-leak").then(({ response, body }) => {
        if (!response.ok) return;
        renderOutputLeakCard(body);
        outputLeakSection.hidden = false;
      }),
    );
  }

  // The audit log spans every resource type, so it requires a full
  // is_admin key (see require_full_admin) rather than any single scope --
  // gated on isFullAdmin, not on adminScopes containing all 5 entries
  // (a key could theoretically be granted all 5 individually without
  // is_admin: true, and that's still not the same authorization boundary).
  if (isFullAdmin) {
    tasks.push(loadAuditLog());
  }

  // Awaited before the keys table, not run in parallel with it: the keys
  // table's per-key "allowed guardrails configs" checkboxes need
  // knownConfigIds, which the guardrails:write fetch above populates.
  await Promise.all(tasks);

  if (adminScopes.includes("keys:write")) {
    const { response, body } = await apiFetch("/api/admin/keys");
    if (response.ok) {
      renderKeysTable(body.keys);
      keysSection.hidden = false;
    }
  }
}

async function refreshAccess() {
  keysSection.hidden = true;
  guardrailsSection.hidden = true;
  promptInjectionSection.hidden = true;
  outputLeakSection.hidden = true;
  auditLogSection.hidden = true;
  keysTableEl.innerHTML = "";
  guardrailsCardsEl.innerHTML = "";
  auditLogTableEl.innerHTML = "";
  piTestResultEl.innerHTML = "";
  olTestResultEl.innerHTML = "";
  createKeyResultEl.innerHTML = "";
  setStatusText(createKeyStatusEl, "");

  const apiKey = currentApiKey();
  if (!apiKey) {
    setStatusText(adminStatusEl, "Enter an API key to continue.");
    return;
  }

  // Deliberately calls whoami first and stops on a no-access result,
  // rather than firing every /api/admin/* request and letting them all
  // 403 -- keeps this page from showing a wall of broken requests for a
  // key with no admin capability at all.
  const { response, body } = await apiFetch("/api/ui/whoami");
  if (!response.ok) {
    setStatusText(adminStatusEl, "Invalid API key.", true);
    return;
  }
  // whoami's admin_scopes is already the *effective* set -- every scope
  // when is_admin is true, exactly the key's own grants otherwise (see
  // APIKeyRecord.effective_admin_scopes()) -- so no separate is_admin
  // branch is needed here.
  const adminScopes = body.admin_scopes || [];
  if (adminScopes.length === 0) {
    setStatusText(
      adminStatusEl,
      `Access denied: key "${body.key_id}" does not have admin access.`,
      true,
    );
    return;
  }

  setStatusText(
    adminStatusEl,
    body.is_admin
      ? `Signed in as admin key "${body.key_id}".`
      : `Signed in as "${body.key_id}" -- scoped access: ${adminScopes.join(", ")}.`,
  );
  await loadAdminPanel(adminScopes, body.is_admin);
}

createKeyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const allowedModels = splitCommaList(createKeyModelsInput.value);
  if (allowedModels.length === 0) {
    setStatusText(createKeyStatusEl, "Allowed models can't be empty.", true);
    return;
  }
  const rpm = parseInt(createKeyRpmInput.value, 10);

  const submitButton = createKeyForm.querySelector("button[type=submit]");
  submitButton.disabled = true;
  setStatusText(createKeyStatusEl, "Creating...");
  createKeyResultEl.innerHTML = "";

  const adminScopes = ALL_ADMIN_SCOPES.filter((scope) => createKeyScopeCheckboxes[scope].checked);

  const { response, body } = await apiFetch("/api/admin/keys", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: createKeyIdInput.value.trim(),
      allowed_models: allowedModels,
      requests_per_minute: Number.isFinite(rpm) ? rpm : undefined,
      is_admin: createKeyAdminInput.checked,
      admin_scopes: adminScopes,
      token_budget_daily: parseOptionalBudget(createKeyBudgetDailyInput),
      token_budget_monthly: parseOptionalBudget(createKeyBudgetMonthlyInput),
    }),
  });
  submitButton.disabled = false;

  if (response.ok) {
    setStatusText(createKeyStatusEl, `Created "${body.key.id}".`);
    renderRawKeyCallout(createKeyResultEl, body.api_key);
    createKeyForm.reset();
    createKeyScopeCheckboxes = renderScopeCheckboxes(createKeyScopesEl, []);
    await loadAdminPanel();
  } else {
    setStatusText(createKeyStatusEl, formatError(body), true);
  }
});

apiKeyInput.addEventListener("change", () => {
  localStorage.setItem(API_KEY_STORAGE_KEY, apiKeyInput.value);
  refreshAccess();
});

apiKeyInput.value = localStorage.getItem(API_KEY_STORAGE_KEY) || "";
refreshAccess();
