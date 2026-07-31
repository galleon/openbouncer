"use strict";

// API_KEY_STORAGE_KEY, escapeHtml, setStatusText, formatError come from common.js.

const apiKeyInput = document.getElementById("api-key");
const adminStatusEl = document.getElementById("admin-status");
const keysSection = document.getElementById("keys-section");
const keysTableEl = document.getElementById("keys-table");
const guardrailsSection = document.getElementById("guardrails-section");
const guardrailsCardsEl = document.getElementById("guardrails-cards");

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
    "<tr><th>Key</th><th>Admin</th><th>Allowed models</th>" +
    "<th>Allowed guardrails configs</th><th></th></tr>";
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const key of keys) {
    const tr = document.createElement("tr");

    const idTd = document.createElement("td");
    idTd.textContent = key.id;
    tr.appendChild(idTd);

    const adminTd = document.createElement("td");
    if (key.is_admin) {
      const badge = document.createElement("span");
      badge.className = "admin-badge";
      badge.textContent = "admin";
      adminTd.appendChild(badge);
    }
    tr.appendChild(adminTd);

    const modelsTd = document.createElement("td");
    modelsTd.textContent = key.allowed_models.join(", ");
    tr.appendChild(modelsTd);

    const configsTd = document.createElement("td");
    configsTd.className = "admin-config-checkboxes";
    // null means unrestricted (this key can set guardrails.config_id to
    // anything, the state every key has until an admin explicitly
    // restricts it) -- distinct from an empty array, which means
    // deliberately restricted to nothing.
    const isUnrestricted = key.allowed_guardrails_configs === null;
    if (isUnrestricted) {
      const note = document.createElement("p");
      note.className = "admin-hint";
      note.textContent = "Unrestricted (any config_id allowed). Check any box to restrict.";
      configsTd.appendChild(note);
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
      configsTd.appendChild(label);
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

    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  keysTableEl.appendChild(table);
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

apiKeyInput.addEventListener("change", () => {
  localStorage.setItem(API_KEY_STORAGE_KEY, apiKeyInput.value);
  refreshAccess();
});

apiKeyInput.value = localStorage.getItem(API_KEY_STORAGE_KEY) || "";
refreshAccess();
