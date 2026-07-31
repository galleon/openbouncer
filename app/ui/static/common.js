"use strict";

// Shared between index.html (chat tester) and admin.html (admin panel).
// Deliberately not a CDN-loaded library: both pages hold a live bearer API
// key (in the API-key field / localStorage), so pulling in third-party JS
// here would be a real exfiltration risk if that script were ever
// compromised or MITM'd.

const API_KEY_STORAGE_KEY = "openbouncer_api_key";

function escapeHtml(str) {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function setStatusText(el, message, isError = false) {
  el.textContent = message;
  el.classList.toggle("error", isError);
}

function formatError(body) {
  if (body && body.error && body.error.message) {
    const parts = [body.error.message];
    if (body.error.type) parts.push(`(${body.error.type}${body.error.code ? `/${body.error.code}` : ""})`);
    return parts.join(" ");
  }
  return JSON.stringify(body, null, 2);
}
