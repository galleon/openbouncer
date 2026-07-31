"use strict";

// API_KEY_STORAGE_KEY, escapeHtml, setStatusText, formatError come from common.js.

const apiKeyInput = document.getElementById("api-key");
const adminStatusEl = document.getElementById("admin-status");
const keysSection = document.getElementById("keys-section");
const keysTableEl = document.getElementById("keys-table");
const guardrailsSection = document.getElementById("guardrails-section");
const guardrailsCardsEl = document.getElementById("guardrails-cards");
const createKeyForm = document.getElementById("create-key-form");
const createKeyIdInput = document.getElementById("create-key-id");
const createKeyModelsInput = document.getElementById("create-key-models");
const createKeyRpmInput = document.getElementById("create-key-rpm");
const createKeyAdminInput = document.getElementById("create-key-admin");
const createKeyStatusEl = document.getElementById("create-key-status");
const createKeyResultEl = document.getElementById("create-key-result");

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

function renderKeysTable(keys) {
  keysTableEl.innerHTML = "";
  const table = document.createElement("table");
  table.className = "admin-table";

  const thead = document.createElement("thead");
  thead.innerHTML =
    "<tr><th>Key</th><th>Admin</th><th>Allowed models</th><th>Requests/min</th>" +
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
      keySaveButton.disabled = true;
      setStatusText(keyRowStatus, "Saving...");
      const { response, body } = await apiFetch(`/api/admin/keys/${encodeURIComponent(key.id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          allowed_models: allowedModels,
          requests_per_minute: Number.isFinite(rpm) ? rpm : undefined,
          is_admin: adminCheckbox.checked,
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

async function loadAdminPanel() {
  const { response: keysResp, body: keysBody } = await apiFetch("/api/admin/keys");
  const { response: configsResp, body: configsBody } = await apiFetch(
    "/api/admin/guardrails/configs",
  );

  if (!keysResp.ok || !configsResp.ok) {
    setStatusText(adminStatusEl, "Failed to load admin data.", true);
    return;
  }

  knownConfigIds = configsBody.configs.map((c) => c.config_id);

  renderKeysTable(keysBody.keys);
  renderGuardrailsCards(configsBody.configs);

  keysSection.hidden = false;
  guardrailsSection.hidden = false;
}

async function refreshAccess() {
  keysSection.hidden = true;
  guardrailsSection.hidden = true;
  keysTableEl.innerHTML = "";
  guardrailsCardsEl.innerHTML = "";
  createKeyResultEl.innerHTML = "";
  setStatusText(createKeyStatusEl, "");

  const apiKey = currentApiKey();
  if (!apiKey) {
    setStatusText(adminStatusEl, "Enter an API key to continue.");
    return;
  }

  // Deliberately calls whoami first and stops on a non-admin result,
  // rather than firing every /api/admin/* request and letting them all
  // 403 -- keeps this page from showing a wall of broken requests for a
  // key that simply isn't an admin key.
  const { response, body } = await apiFetch("/api/ui/whoami");
  if (!response.ok) {
    setStatusText(adminStatusEl, "Invalid API key.", true);
    return;
  }
  if (!body.is_admin) {
    setStatusText(
      adminStatusEl,
      `Access denied: key "${body.key_id}" does not have admin access.`,
      true,
    );
    return;
  }

  setStatusText(adminStatusEl, `Signed in as admin key "${body.key_id}".`);
  await loadAdminPanel();
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

  const { response, body } = await apiFetch("/api/admin/keys", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: createKeyIdInput.value.trim(),
      allowed_models: allowedModels,
      requests_per_minute: Number.isFinite(rpm) ? rpm : undefined,
      is_admin: createKeyAdminInput.checked,
    }),
  });
  submitButton.disabled = false;

  if (response.ok) {
    setStatusText(createKeyStatusEl, `Created "${body.key.id}".`);
    renderRawKeyCallout(createKeyResultEl, body.api_key);
    createKeyForm.reset();
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
