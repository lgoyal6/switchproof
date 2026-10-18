"""Browser checks against existing receipts. Never starts a paid planner run."""

import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://127.0.0.1:18765")
parser.add_argument("--screenshots", type=Path, required=True)
args = parser.parse_args()
args.screenshots.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    runs = page.request.get(args.url + "/api/runs").json()
    verified = next((r for r in runs if r["status"] == "verified"), None)
    assert verified, "A completed verified receipt is required for this browser check"
    report = page.request.get(args.url + "/api/runs/" + verified["id"]).json()
    page.goto(args.url)
    expect(page.get_by_role("heading", name="Neo4j to FalkorDB")).to_be_visible()
    page.get_by_label("Investigation history").select_option(verified["id"])
    expect(page.get_by_role("heading", name="Ready for the tested workload")).to_be_visible()
    expect(page.get_by_role("button", name="Run demo", exact=True)).to_be_enabled()
    expect(page.get_by_role("heading", name="Repair trials")).to_be_visible()
    query_items = page.locator(".query-item")
    expect(query_items).to_have_count(4)
    expect(page.get_by_role("heading", name="Preserve source-facing entity IDs")).to_be_visible()
    page.screenshot(path=str(args.screenshots / "query-review-desktop.png"), full_page=True)
    page.get_by_role("button", name="Result evidence", exact=True).click()
    expect(page.get_by_text("Expected from Neo4j", exact=True)).to_be_visible()
    expect(page.get_by_text("Imported result, before repair", exact=True)).to_be_visible()
    page.get_by_role("button", name="Query comparison", exact=True).click()
    page.get_by_role("button", name="All 12", exact=True).click()
    expect(query_items).to_have_count(12)
    page.get_by_label("Search queries").fill("ordered roles")
    expect(query_items).to_have_count(1)
    query_items.first.click()
    expect(page.get_by_text("Data repaired, query unchanged", exact=True)).to_be_visible()
    page.get_by_label("Search queries").fill("no-such-query")
    expect(page.get_by_text("No queries match your search.")).to_be_visible()
    page.get_by_role("button", name="Clear query search").click()
    page.get_by_role("button", name="Unverified 1", exact=True).click()
    expect(query_items).to_have_count(1)
    expect(page.get_by_role("heading", name="This query needs a result contract")).to_be_visible()
    expect(page.get_by_text("Result hashes match", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Changed 4", exact=True).click()
    page.get_by_role("button", name="What this does", exact=True).click()
    expect(page.get_by_text("Current scope: the Movies demo", exact=True)).to_be_visible()
    page.screenshot(path=str(args.screenshots / "product-explanation.png"), full_page=True)
    page.get_by_role("button", name="What this does", exact=True).click()
    page.get_by_role("button", name="Run demo", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("heading", name="Run the Movies demo")).to_be_visible()
    expect(dialog.get_by_label("Repair planner")).to_have_value("anthropic")
    expect(dialog.get_by_role("button", name="Run investigation", exact=True)).to_be_enabled()
    page.screenshot(path=str(args.screenshots / "run-dialog.png"))
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    negative = page.locator(".repair-trial.rejected")
    negative.locator("summary").click()
    expect(
        negative.get_by_text(
            "This candidate changed the output contract and was refused, even though its query ran."
        )
    ).to_be_visible()
    negative.locator("summary").click()
    for tab in ["Application", "Dependency graph", "Performance", "Run log"]:
        page.get_by_role("button", name=tab, exact=False).filter(has_text=tab).first.click()
        expect(page.get_by_role("button", name=tab, exact=False).first).to_have_attribute(
            "aria-current", "page"
        )
        if tab == "Dependency graph":
            expect(page.locator('svg[role="img"]')).to_be_visible()
            page.get_by_role("button", name="Cast and ordered roles", exact=True).click()
        if tab == "Application":
            expect(page.get_by_text("vote persisted state", exact=True)).to_be_visible()
            expect(page.locator(".endpoint-row")).to_have_count(len(report["application"]["checks"]))
        if tab == "Performance":
            expect(page.get_by_role("columnheader", name="FalkorDB median")).to_be_visible()
            expect(page.locator(".performance-table tbody tr")).to_have_count(11)
        page.screenshot(path=str(args.screenshots / (tab.lower().replace(" ", "-") + ".png")), full_page=True)
    with page.expect_download() as receipt:
        page.get_by_role("link", name="Export report").click()
    assert receipt.value.suggested_filename == f"switchproof-{verified['id']}.json"
    page.get_by_role("button", name="Query review", exact=False).click()
    page.get_by_role("button", name="Changed 4", exact=True).click()
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_label("Investigation history")).to_be_visible()
    page.screenshot(path=str(args.screenshots / "query-review-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "Page overflows horizontally"
    )
    page.get_by_role("button", name="Run demo", exact=True).click()
    expect(page.get_by_role("dialog")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("button", name="Cancel", exact=True).click()
    failed = next((r for r in runs if r["status"] == "error"), None)
    if failed:
        page.get_by_label("Investigation history").select_option(failed["id"])
        expect(page.get_by_role("heading", name="Investigation stopped", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="Ready for the tested workload")).to_have_count(0)
        page.screenshot(path=str(args.screenshots / "error-mobile.png"), full_page=True)
    page.get_by_label("Investigation history").select_option(verified["id"])
    expect(page.get_by_role("heading", name="Ready for the tested workload")).to_be_visible()
    for tab in ["Application", "Dependency graph", "Performance", "Run log"]:
        page.get_by_role("button", name=tab, exact=False).first.click()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), tab

    # Exercise a rejected start without sending a mutation to the server or invoking a model.
    def busy_response(route):
        if route.request.method == "POST":
            route.fulfill(
                status=409,
                json={"detail": "An investigation is already using the disposable source database"},
            )
        else:
            route.continue_()

    page.route("**/api/runs", busy_response)
    page.get_by_role("button", name="Run demo", exact=True).click()
    page.get_by_role("button", name="Run investigation", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.get_by_role("alert")).to_contain_text("already using")
    page.unroute("**/api/runs", busy_response)
    # Empty state is an isolated browser fixture, not a claimed database or model result.
    page.route("**/api/runs", lambda route: route.fulfill(json=[]))
    page.reload()
    expect(page.get_by_role("heading", name="Test a migration before making the switch")).to_be_visible()
    expect(page.get_by_role("heading", name="Ready for the tested workload")).to_have_count(0)
    page.screenshot(path=str(args.screenshots / "empty-mobile.png"), full_page=True)
    assert not errors, errors
    # No API writes, model calls or database reseeds occurred in this smoke check.
    print(
        "PASS: query filters/search, typed evidence, scope explanation, accessible settings dialog, "
        "negative control, five sections, graph selection, receipt download, history, mobile, error/empty states and rejected-run feedback"
    )
    browser.close()
