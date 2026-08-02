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

  const apiKey = currentApiKey();
  if (!apiKey) {
    setStatusText(activityStatusEl, "Enter an API key to continue.");
    return;
  }

  // Same pattern as admin.js: check whoami first so a non-admin key gets a
  // clean denied message instead of a broken/empty dashboard.
  const { response, body } = await apiFetch("/api/ui/whoami");
  if (!response.ok) {
    setStatusText(activityStatusEl, "Invalid API key.", true);
    return;
  }
  if (!body.is_admin) {
    setStatusText(
      activityStatusEl,
      `Access denied: key "${body.key_id}" does not have admin access.`,
      true,
    );
    return;
  }

  setStatusText(activityStatusEl, "Loading...");
  await loadActivity();
}

apiKeyInput.addEventListener("change", () => {
  localStorage.setItem(API_KEY_STORAGE_KEY, apiKeyInput.value);
  refreshAccess();
});
rangeSelect.addEventListener("change", refreshAccess);

apiKeyInput.value = localStorage.getItem(API_KEY_STORAGE_KEY) || "";
refreshAccess();
