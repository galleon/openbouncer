"""Browser-based smoke tests for the three static UI pages
(app/ui/static/{index,admin,activity}.html). Opt-in -- see conftest.py's
module docstring for how to run these and why they're excluded by default.

Deliberately narrow in scope: does each page load without a JS error,
does the persistent shell (sidebar/topbar, built by shell.js) render, and
does entering a real admin key actually unlock the sections gated on it
-- exactly the class of bug a curl-based check can't catch, since it
never runs any of this project's own JavaScript. This is not a general
UI test suite: it doesn't drive the chat form, the admin CRUD forms, or
the guardrails editors -- those are better covered by more targeted
Playwright tests if/when a specific page grows a history of breaking
silently.
"""

import pytest

pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.e2e


def test_chat_tester_loads(page, live_server_url):
    # Uncaught JS exceptions, not console noise -- a failed fetch (e.g. the
    # Activity page's Prometheus-backed endpoint, expected to 503 when no
    # PROMETHEUS_URL is configured for this throwaway server) is handled
    # gracefully by the app's own JS and shows up as a console-level
    # resource-load error regardless, which would make that a false
    # positive here. An uncaught exception is what actually indicates the
    # page's JS broke.
    page_errors = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    page.goto(f"{live_server_url}/ui/index.html")

    assert page.title() == "OpenBouncer"
    assert page.locator(".app-sidebar-brand").inner_text() == "OpenBouncer"
    assert page.locator("#api-key").count() == 1
    assert page.locator("#chat-form").count() == 1
    assert page.locator("#model").count() == 1
    assert page_errors == []


def test_admin_panel_unlocks_with_a_real_admin_key(page, live_server_url, admin_raw_key):
    # See test_chat_tester_loads for why this tracks uncaught exceptions
    # rather than console messages.
    page_errors = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    page.goto(f"{live_server_url}/ui/admin.html")
    assert page.title() == "OpenBouncer Admin"

    # Sections start hidden (see the `hidden` attribute in admin.html) and
    # stay that way with no key entered -- admin.js's refreshAccess()
    # returns early in that case.
    assert page.locator("#keys-section").is_hidden()

    page.fill("#api-key", admin_raw_key)
    page.locator("#api-key").dispatch_event("change")

    # This is the real whoami -> scope-check -> section-unhide flow
    # (admin.js's refreshAccess()/loadAdminPanel()) running against a real
    # server, not a mock.
    page.wait_for_selector("#keys-section:not([hidden])", timeout=5000)
    assert "Signed in as admin key" in page.locator("#admin-status").inner_text()

    # The seeded e2e-admin key itself should render as a row in the table
    # admin.js's renderKeysTable() builds.
    page.wait_for_selector("#keys-table table")
    assert "e2e-admin" in page.locator("#keys-table").inner_text()

    # is_admin: true implies every scope, so the Prompt Injection and
    # Output Leak cards render too -- both build their Category/Action
    # table via admin.js's buildCategoryActionTable(), which (like
    # renderKeysTable() above) is built on the shared renderTable()
    # helper, so this exercises that shared code twice over, not just via
    # the keys table.
    page.wait_for_selector("#prompt-injection-section:not([hidden])")
    assert page.locator("#pi-categories table tr").count() > 1
    page.wait_for_selector("#output-leak-section:not([hidden])")
    assert page.locator("#ol-categories table tr").count() > 1

    assert page_errors == []


def test_activity_dashboard_loads(page, live_server_url, admin_raw_key):
    # See test_chat_tester_loads for why this tracks uncaught exceptions
    # rather than console messages -- this page in particular is expected
    # to log a handled 503 (no PROMETHEUS_URL for this throwaway server),
    # which a console-message check would wrongly flag.
    page_errors = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    page.goto(f"{live_server_url}/ui/activity.html")
    assert page.title() == "OpenBouncer Activity"

    page.fill("#api-key", admin_raw_key)
    page.locator("#api-key").dispatch_event("change")

    # No PROMETHEUS_URL is configured for this e2e server, so the
    # Prometheus-backed charts stay in their "not configured" state -- but
    # the Guardrail Events table is deliberately independent of that (see
    # activity.js) and should still unhide and render.
    page.wait_for_selector("#guardrail-events-section:not([hidden])", timeout=5000)
    assert page.locator("#guardrail-events-table").count() == 1

    assert page_errors == []
