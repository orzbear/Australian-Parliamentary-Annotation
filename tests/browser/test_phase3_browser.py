from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright

BASE_URL = os.getenv("HANSARD_BROWSER_BASE_URL")
USERNAME = os.getenv("HANSARD_BROWSER_USERNAME")
PASSWORD = os.getenv("HANSARD_BROWSER_PASSWORD")
pytestmark = pytest.mark.skipif(
    not (BASE_URL and USERNAME and PASSWORD),
    reason="Phase 3 browser acceptance environment is required",
)
CONFIG = json.loads(Path("playwright.config.json").read_text(encoding="utf-8"))
SCREENSHOTS = Path(CONFIG["screenshots"])


def _login(page: Page) -> None:
    assert BASE_URL and USERNAME and PASSWORD
    page.goto(f"{BASE_URL}/login")
    page.get_by_label("Username").fill(USERNAME)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in securely").click()
    if page.url.endswith("/change-password"):
        page.get_by_label("New password").fill(PASSWORD)
        page.get_by_label("Confirm password").fill(PASSWORD)
        page.get_by_role("button", name="Update password").click()
        page.get_by_label("Username").fill(USERNAME)
        page.get_by_label("Password").fill(PASSWORD)
        page.get_by_role("button", name="Sign in securely").click()
    expect(page.get_by_role("heading", name="Good to see you,")).to_be_visible()


def _safe_workspace(page: Page) -> None:
    page.locator(".speech-text").evaluate(
        "(element) => element.textContent = "
        "'Synthetic browser-test speech.\\n\\nNo private corpus text is shown.'"
    )
    page.locator("#speech-heading").evaluate(
        "(element) => element.textContent = 'Synthetic Test Speaker'"
    )


def _safe_dashboard(page: Page) -> None:
    page.locator(".queue-row").evaluate_all(
        """rows => rows.forEach((row, index) => {
          const name = row.querySelector('strong');
          const detail = row.querySelector('p');
          if (name) name.textContent = `Synthetic Speaker ${index + 1}`;
          if (detail) detail.textContent = 'Synthetic browser-test task';
        })"""
    )


def _safe_project(page: Page) -> None:
    page.locator("section").evaluate_all(
        """sections => sections.forEach(section => {
          if (section.textContent.includes('Allocate available work')) {
            section.querySelectorAll('form > strong').forEach(
              (name, index) => name.textContent = `Synthetic Speaker ${index + 1}`
            );
            section.querySelectorAll('form > p').forEach(
              detail => detail.textContent = 'Synthetic task · browser acceptance'
            );
          }
        })"""
    )


def _complete_form(page: Page) -> None:
    page.locator('input[name="primary_australian_domain"][value="AU03"]').check()
    page.get_by_label("Add secondary topics").check()
    page.locator(
        'label.choice-chip:has(input[name="secondary_australian_domains"][value="AU05"])'
    ).click()


def test_phase3_real_browser_workflow_and_screenshots() -> None:
    assert BASE_URL
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=CONFIG["headless"])
        desktop = browser.new_context(viewport=CONFIG["desktop_viewport"])
        page = desktop.new_page()
        page.goto(f"{BASE_URL}/login")
        page.screenshot(path=SCREENSHOTS / "login-desktop.png", full_page=True)
        _login(page)
        _safe_dashboard(page)
        page.screenshot(path=SCREENSHOTS / "dashboard-desktop.png", full_page=True)

        page.get_by_role("link", name="Projects").click()
        page.locator("a.card-link", has_text="Conference general policy pass").click()
        _safe_project(page)
        page.screenshot(path=SCREENSHOTS / "project-overview-desktop.png", full_page=True)
        page.get_by_role("button", name="Claim next task").click()
        _safe_workspace(page)
        page.screenshot(path=SCREENSHOTS / "annotation-workspace-desktop.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

        page.get_by_role("button", name="Submit & next").click()
        expect(page.locator("#error-summary")).to_be_visible()
        _safe_workspace(page)
        page.screenshot(path=SCREENSHOTS / "validation-error.png", full_page=True)

        _complete_form(page)
        page.keyboard.press("Control+s")
        expect(page.locator("#save-status")).to_contain_text("Draft saved")
        assignment_url = page.locator("#annotation-form").get_attribute("action")
        assert assignment_url
        page.goto(f"{BASE_URL}{assignment_url.removesuffix('/submit')}")
        expect(
            page.locator('input[name="primary_australian_domain"][value="AU03"]')
        ).to_be_checked()
        page.keyboard.press("Control+Enter")
        expect(page.get_by_text("Annotation submitted.")).to_be_visible()
        _safe_workspace(page)
        page.screenshot(path=SCREENSHOTS / "submitted-success.png", full_page=True)
        page.get_by_role("button", name="Skip for now").click()

        page.get_by_role("link", name="Projects").click()
        page.locator("a.card-link", has_text="Conference general policy pass").click()
        page.get_by_role("button", name="Claim next task").click()
        page.get_by_label("Flag reason").fill("Synthetic browser test flag")
        page.get_by_role("button", name="Flag").click()

        mobile = browser.new_context(viewport=CONFIG["mobile_viewport"])
        mobile_page = mobile.new_page()
        _login(mobile_page)
        mobile_page.get_by_role("link", name="Projects").click()
        mobile_page.locator(
            "a.card-link", has_text="Conference general policy pass"
        ).click()
        mobile_page.get_by_role("button", name="Claim next task").click()
        _safe_workspace(mobile_page)
        mobile_page.screenshot(
            path=SCREENSHOTS / "annotation-workspace-mobile.png", full_page=True
        )
        assert mobile_page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        mobile.close()
        desktop.close()
        browser.close()


def test_conference_two_pass_browser_prototype() -> None:
    assert BASE_URL
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=CONFIG["headless"])
        context = browser.new_context(viewport=CONFIG["desktop_viewport"])
        page = context.new_page()
        _login(page)
        page.get_by_role("link", name="Projects").click()
        page.locator(
            "a.card-link", has_text="Conference general policy pass"
        ).click()
        page.get_by_role("button", name="Claim next task").click()
        _safe_workspace(page)
        expect(
            page.get_by_role("heading", name="Australian Policy Annotation")
        ).to_be_visible()
        non_policy = page.get_by_label("This speech is entirely non-policy")
        primary = page.locator('[data-field="primary_australian_domain"]')
        secondary_content = page.locator(
            '[data-reveal-content="secondary_australian_domains"]'
        )
        expect(primary).to_be_visible()
        expect(secondary_content).to_be_hidden()
        page.get_by_label("Add secondary topics").check()
        expect(secondary_content).to_be_visible()
        non_policy.check()
        expect(primary).to_be_hidden()
        expect(page.get_by_role("button", name="Mark non-policy & next")).to_be_visible()
        non_policy.uncheck()
        page.locator(
            'input[name="primary_australian_domain"][value="AU12"]'
        ).check()
        page.get_by_role("button", name="Submit & next").click()
        expect(page.get_by_text("Annotation submitted.")).to_be_visible()

        page.get_by_role("link", name="Projects").click()
        page.locator(
            "a.card-link", has_text="Conference AUKUS second pass"
        ).click()
        page.get_by_role("button", name="Generate tasks").click()
        page.get_by_role("button", name="Claim next task").click()
        if "/assignments/" not in page.url:
            page.get_by_role("link", name="Dashboard").click()
            page.locator(
                "a.queue-row", has_text="Conference AUKUS second pass"
            ).first.click()
        _safe_workspace(page)
        expect(
            page.get_by_role("heading", name="Australian AUKUS Screen")
        ).to_be_visible()
        page.get_by_text("Source and technical provenance").click()
        expect(page.get_by_text("source annotation version")).to_be_visible()
        page.get_by_label("This speech substantively discusses AUKUS").check()
        page.get_by_role("button", name="Submit & next").click()
        context.close()
        browser.close()
