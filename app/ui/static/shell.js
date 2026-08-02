"use strict";

// Builds the persistent app chrome (top bar + sidebar) around each page's
// own <main id="page-main">, and owns the two things every page shares:
// the API-key field (already used by common.js's API_KEY_STORAGE_KEY) and
// the light/dark/system theme toggle. Runs synchronously at load time,
// before any page-specific script (admin.js/app.js/activity.js) -- those
// still do plain document.getElementById("api-key") calls, which only
// work if this has already run and created that element. Keep the
// <script src="shell.js"> tag before the page's own script tag in every
// HTML file.

const THEME_STORAGE_KEY = "openbouncer_theme";

const NAV_ITEMS = [
  { id: "activity", label: "Activity", href: "activity.html" },
  { id: "keys", label: "API Keys", href: "admin.html#keys-section" },
  { id: "guardrails", label: "Guardrails", href: "admin.html#guardrails-section" },
  { id: "chat", label: "Chat Tester", href: "index.html" },
];

const THEME_OPTIONS = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "system", label: "System" },
];

function applyTheme(value) {
  if (value === "system") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", value);
  }
}

function currentTheme() {
  return localStorage.getItem(THEME_STORAGE_KEY) || "system";
}

function buildThemeToggle() {
  const wrapper = document.createElement("div");
  wrapper.className = "theme-toggle";
  wrapper.setAttribute("role", "group");
  wrapper.setAttribute("aria-label", "Theme");

  const buttons = {};
  for (const option of THEME_OPTIONS) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "theme-toggle-option";
    button.textContent = option.label;
    button.title = option.label;
    button.addEventListener("click", () => {
      localStorage.setItem(THEME_STORAGE_KEY, option.value);
      applyTheme(option.value);
      for (const [value, el] of Object.entries(buttons)) {
        el.classList.toggle("active", value === option.value);
      }
    });
    buttons[option.value] = button;
    wrapper.appendChild(button);
  }

  const active = currentTheme();
  if (buttons[active]) {
    buttons[active].classList.add("active");
  }

  return wrapper;
}

function buildSidebar(activeNavId) {
  const nav = document.createElement("nav");
  nav.className = "app-sidebar";

  const brand = document.createElement("div");
  brand.className = "app-sidebar-brand";
  brand.textContent = "OpenBouncer";
  nav.appendChild(brand);

  const list = document.createElement("ul");
  list.className = "app-sidebar-nav";
  for (const item of NAV_ITEMS) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = item.href;
    a.textContent = item.label;
    if (item.id === activeNavId) {
      a.className = "active";
      a.setAttribute("aria-current", "page");
    }
    li.appendChild(a);
    list.appendChild(li);
  }
  nav.appendChild(list);

  return nav;
}

function buildTopbar(pageTitle) {
  const header = document.createElement("header");
  header.className = "app-topbar";

  const title = document.createElement("div");
  title.className = "app-topbar-title";
  title.textContent = pageTitle;
  header.appendChild(title);

  const actions = document.createElement("div");
  actions.className = "app-topbar-actions";

  const keyField = document.createElement("label");
  keyField.className = "api-key-field";
  const keySpan = document.createElement("span");
  keySpan.textContent = "API key";
  const keyInput = document.createElement("input");
  keyInput.type = "password";
  keyInput.id = "api-key";
  keyInput.placeholder = "sk-...";
  keyInput.autocomplete = "off";
  keyInput.spellcheck = false;
  keyField.appendChild(keySpan);
  keyField.appendChild(keyInput);

  actions.appendChild(keyField);
  actions.appendChild(buildThemeToggle());
  header.appendChild(actions);

  return header;
}

(function initShell() {
  applyTheme(currentTheme());

  const pageMain = document.getElementById("page-main");
  if (!pageMain) {
    // Every shell-using page must have <main id="page-main">; fail loudly
    // in the console rather than silently rendering a chrome-less page.
    console.error("shell.js: no #page-main found, skipping shell setup");
    return;
  }

  const activeNavId = document.body.dataset.nav || "";
  const pageTitle = document.body.dataset.pageTitle || "OpenBouncer";

  const shell = document.createElement("div");
  shell.className = "app-shell";

  const body = document.createElement("div");
  body.className = "app-body";

  const contentCol = document.createElement("div");
  contentCol.className = "app-content";
  contentCol.appendChild(pageMain);

  body.appendChild(buildSidebar(activeNavId));
  body.appendChild(contentCol);

  shell.appendChild(buildTopbar(pageTitle));
  shell.appendChild(body);

  document.body.appendChild(shell);
})();
