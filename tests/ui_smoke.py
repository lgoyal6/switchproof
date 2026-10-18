"""Browser check against an existing real receipt. Does not start a paid planner run."""

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
    page.goto(args.url)
    expect(page.get_by_role("heading", name="Make the switch. Keep the behavior.")).to_be_visible()
    expect(page.get_by_role("button", name="Run investigation", exact=True)).to_be_enabled()
    expect(page.get_by_text("Repairs must earn a pass")).to_be_visible()
    page.screenshot(path=str(args.screenshots / "investigation-desktop.png"), full_page=True)
    for tab in ["Dependency graph", "Application", "Performance", "Activity"]:
        page.get_by_role("button", name=tab, exact=True).click()
        expect(page.get_by_role("button", name=tab, exact=True)).to_have_attribute("aria-current", "page")
        if tab == "Dependency graph":
            expect(page.locator('svg[role="img"]')).to_be_visible()
            page.get_by_role("button", name="Cast and ordered roles", exact=True).click()
        if tab == "Application":
            expect(page.get_by_text("vote persisted state", exact=True)).to_be_visible()
        if tab == "Performance":
            expect(page.get_by_text("Median client latency in milliseconds")).to_be_visible()
        page.screenshot(path=str(args.screenshots / (tab.lower().replace(" ", "-") + ".png")), full_page=True)
    with page.expect_download() as receipt:
        page.get_by_role("link", name="Export evidence").click()
    assert receipt.value.suggested_filename.startswith("switchproof-")
    page.get_by_role("button", name="Investigation", exact=True).click()
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(args.screenshots / "investigation-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "Page overflows horizontally"
    )
    expect(page.get_by_role("button", name="Run investigation", exact=True)).to_be_visible()
    assert not errors, errors
    print("PASS: five evidence sections, graph selection, receipt download, mobile width, no page errors")
    browser.close()
