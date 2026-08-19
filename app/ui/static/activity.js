"use strict";

// API_KEY_STORAGE_KEY, setStatusText, formatError come from common.js.
// renderTimeSeries comes from charts.js.

const apiKeyInput = document.getElementById("api-key");
const activityStatusEl = document.getElementById("activity-status");
const activityContentEl = document.getElementById("activity-content");
const rangeSelect = document.getElementById("activity-range");
const statCardsEl = document.getElementById("stat-cards");
const requestsChartEl = document.getElementById("requests-chart");
const topKeysListEl = document.getElementById("top-keys-list");
const topModelsListEl = document.getElementById("top-models-list");
const geFilterKeyInput = document.getElementById("ge-filter-key");
const geFilterGuardrailSelect = document.getElementById("ge-filter-guardrail");
const geFilterActionSelect = document.getElementById("ge-filter-action");
const geFilterApplyButton = document.getElementById("ge-filter-apply");
const guardrailEventsTableEl = document.getElementById("guardrail-events-table");
const guardrailEventsSectionEl = document.getElementById("guardrail-events-section");

function currentApiKey() {
  return apiKeyInput.value.trim();
}

async function apiFetch(path) {
  const response = await fetch(path, {
    headers: { Authorization: `Bearer ${currentApiKey()}` },
  });
  let body = null;
  try {
    body = await response.json();
  } catch (err) {
    // No JSON body (e.g. a network-level failure) -- callers check response.ok.
  }
  return { response, body };
}

function formatNumber(n) {
  return new Intl.NumberFormat().format(Math.round(n));
}

function renderStatCards(totals) {
  statCardsEl.innerHTML = "";
  const cards = [
    { label: "Requests", value: formatNumber(totals.requests) },
    { label: "Token volume", value: formatNumber(totals.tokens) },
    {
      label: "Success rate",
      value:
        totals.success_rate === null ? "–" : `${(totals.success_rate * 100).toFixed(1)}%`,
    },
    {
      label: "Avg latency",
      value:
        totals.avg_latency_seconds === null
          ? "–"
          : `${totals.avg_latency_seconds.toFixed(2)}s`,
    },
  ];
  for (const card of cards) {
    const el = document.createElement("div");
    el.className = "stat-card";
    const label = document.createElement("div");
    label.className = "stat-card-label";
    label.textContent = card.label;
    const value = document.createElement("div");
    value.className = "stat-card-value";
    value.textContent = card.value;
    el.appendChild(label);
    el.appendChild(value);
    statCardsEl.appendChild(el);
  }
}

function renderRankedList(container, items, labelKey, valueKey, emptyMessage) {
  container.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "admin-hint";
    empty.textContent = emptyMessage;
    container.appendChild(empty);
    return;
  }
  const list = document.createElement("ol");
  list.className = "activity-list";
  for (const item of items) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = item[labelKey];
    const value = document.createElement("span");
    value.className = "activity-list-value";
    value.textContent = formatNumber(item[valueKey]);
    li.appendChild(label);
    li.appendChild(value);
    list.appendChild(li);
  }
  container.appendChild(list);
}

function renderRequestsChart(requestsByModel) {
  requestsChartEl.innerHTML = "";
  if (requestsByModel.length === 0) {
    const empty = document.createElement("p");
    empty.className = "admin-hint";
    empty.textContent = "No request activity in this range yet.";
    requestsChartEl.appendChild(empty);
    return;
  }
  renderTimeSeries(requestsChartEl, {
    series: requestsByModel.map((s) => ({ label: s.model, points: s.points })),
    yLabel: "Requests",
  });
}

function renderGuardrailEventsTable(events) {
  guardrailEventsTableEl.innerHTML = "";
  if (events.length === 0) {
    const empty = document.createElement("p");
    empty.className = "admin-hint";
    empty.textContent = "No guardrail events match this filter yet.";
    guardrailEventsTableEl.appendChild(empty);
    return;
  }

  const table = document.createElement("table");
  table.className = "admin-table";
  const thead = document.createElement("thead");
  thead.innerHTML =
    "<tr><th>Time</th><th>Key</th><th>Guardrail</th><th>Model</th><th>Category</th><th>Action</th><th>Snippet</th></tr>";
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const event of events) {
    const tr = document.createElement("tr");
    for (const value of [
      event.timestamp,
      event.key_id,
      event.guardrail,
      event.model,
      event.category,
      event.action,
      event.snippet,
    ]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);

  const scrollWrapper = document.createElement("div");
  scrollWrapper.className = "admin-table-scroll";
  scrollWrapper.appendChild(table);
  guardrailEventsTableEl.appendChild(scrollWrapper);
}

async function loadGuardrailEvents() {
  const params = new URLSearchParams({ limit: "50" });
  const keyId = geFilterKeyInput.value.trim();
  if (keyId) params.set("key_id", keyId);
  if (geFilterGuardrailSelect.value) params.set("guardrail", geFilterGuardrailSelect.value);
  if (geFilterActionSelect.value) params.set("action", geFilterActionSelect.value);

  const { response, body } = await apiFetch(`/api/admin/guardrail-events?${params.toString()}`);
  if (!response.ok) return;
  renderGuardrailEventsTable(body.events);
}

geFilterApplyButton.addEventListener("click", loadGuardrailEvents);

async function loadActivity() {
  const range = rangeSelect.value;
  const { response, body } = await apiFetch(`/api/admin/activity/overview?range=${range}`);

  if (!response.ok) {
    activityContentEl.hidden = true;
    const code = body && body.error && body.error.code;
    if (code === "observability_not_configured") {
      setStatusText(
        activityStatusEl,
        "Observability isn't configured for this deployment (no PROMETHEUS_URL set).",
        true,
      );
    } else if (code === "observability_unavailable") {
      setStatusText(
        activityStatusEl,
        "The observability backend is unreachable right now -- is Prometheus running?",
        true,
      );
    } else {
      setStatusText(activityStatusEl, formatError(body), true);
    }
    return;
  }

  setStatusText(activityStatusEl, "");
  activityContentEl.hidden = false;

  renderStatCards(body.totals);
  renderRequestsChart(body.requests_by_model);
  renderRankedList(topKeysListEl, body.top_keys_by_tokens, "key_id", "tokens", "No usage recorded yet.");
  renderRankedList(
    topModelsListEl,
    body.top_models_by_requests,
    "model",
    "requests",
    "No requests recorded yet.",
  );
}

async function refreshAccess() {
  activityContentEl.hidden = true;
  guardrailEventsSectionEl.hidden = true;

  const apiKey = currentApiKey();
  if (!apiKey) {
    setStatusText(activityStatusEl, "Enter an API key to continue.");
    return;
  }

  // Same pattern as admin.js: check whoami first so a key without the
  // "activity:read" admin scope gets a clean denied message instead of a
  // broken/empty dashboard. whoami's admin_scopes is already the
  // *effective* set (every scope when is_admin is true), so a full admin
  // key satisfies this the same way a scoped observer key does.
  const { response, body } = await apiFetch("/api/ui/whoami");
  if (!response.ok) {
    setStatusText(activityStatusEl, "Invalid API key.", true);
    return;
  }
  if (!(body.admin_scopes || []).includes("activity:read")) {
    setStatusText(
      activityStatusEl,
      `Access denied: key "${body.key_id}" does not have the "activity:read" admin scope.`,
      true,
    );
    return;
  }

  setStatusText(activityStatusEl, "Loading...");
  // Independent of loadActivity()'s Prometheus-backed data -- this table
  // reads the local guardrail event log directly, so it loads (and stays
  // visible) even when PROMETHEUS_URL isn't configured for this
  // deployment and the rest of the page can't render.
  guardrailEventsSectionEl.hidden = false;
  await Promise.all([loadActivity(), loadGuardrailEvents()]);
}

apiKeyInput.addEventListener("change", () => {
  localStorage.setItem(API_KEY_STORAGE_KEY, apiKeyInput.value);
  refreshAccess();
});
rangeSelect.addEventListener("change", refreshAccess);

apiKeyInput.value = localStorage.getItem(API_KEY_STORAGE_KEY) || "";
refreshAccess();
