"""End-to-end tests: drive the web UI in Chrome against a scripted directory."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "scenarios"


def choose(page, sample):
    page.set_input_files("#file-input", str(SAMPLES / sample))


def create(page, sample):
    choose(page, sample)
    page.click("#create")


def test_valid_file_is_reviewed_then_goes_live(page, directory):
    choose(page, "02-valid-3-hospitals.csv")

    expect(page.locator("#review-title")).to_have_text("02-valid-3-hospitals.csv")
    expect(page.locator("#review-verdict")).to_have_text("3 hospitals ready to import")
    expect(page.locator("#preview-body tr")).to_have_count(3)

    page.click("#create")

    expect(page.locator("#batch-title")).to_have_text("3 hospitals live", timeout=10_000)
    expect(page.locator('.cell[data-s="created_and_activated"]')).to_have_count(3)
    expect(page.locator("#m-live")).to_have_text("Yes")
    expect(page.locator(".batch-item")).to_have_count(1)
    assert len(directory.hospitals) == 3


def test_invalid_file_lists_every_problem_and_cannot_be_created(page, directory):
    choose(page, "21-invalid-many-problems-at-once.csv")

    expect(page.locator("#review-verdict")).to_have_text("4 problems to fix. Nothing has been created.")
    expect(page.locator("#review-errors li")).to_have_count(4)
    expect(page.locator("#create")).to_be_hidden()
    expect(page.locator("#preview-panel")).to_be_hidden()
    assert directory.count("POST") == 0


def test_warnings_are_shown_but_do_not_block_import(page):
    choose(page, "12-warning-duplicate-rows.csv")

    expect(page.locator("#review-warnings li")).to_have_count(2)
    expect(page.locator("#create")).to_be_enabled()


def test_partial_failure_can_be_retried_to_completion(page):
    create(page, "D1-partial-failure-then-retry.csv")

    expect(page.locator("#batch-title")).to_have_text("2 hospitals failed", timeout=10_000)
    expect(page.locator('.cell[data-s="failed"]')).to_have_count(2)
    expect(page.locator("#rows-body tr.is-failed .error-text").first).to_contain_text("Hospital Directory")
    expect(page.locator("#m-live")).to_have_text("No")

    page.click("#resume")

    expect(page.locator("#batch-title")).to_have_text("15 hospitals live", timeout=10_000)
    expect(page.locator("#resume")).to_be_hidden()


def test_activation_failure_can_be_retried(page):
    create(page, "D4-activation-fails-then-retry.csv")

    expect(page.locator("#batch-title")).to_have_text("Created, but not activated", timeout=10_000)
    expect(page.locator("#resume")).to_have_text("Retry activation")

    page.click("#resume")

    expect(page.locator("#batch-title")).to_have_text("6 hospitals live", timeout=10_000)


def test_rollback_needs_a_second_click_to_confirm(page, directory):
    create(page, "03-valid-1-hospital.csv")
    expect(page.locator("#batch-title")).to_have_text("1 hospital live", timeout=10_000)

    page.click("#rollback")
    expect(page.locator("#rollback")).to_have_text("Delete 1 hospital")
    assert len(directory.hospitals) == 1  # nothing deleted on the first click

    page.click("#rollback")

    expect(page.locator("#batch-title")).to_have_text("Batch rolled back")
    expect(page.locator("#rollback")).to_be_hidden()
    assert directory.hospitals == {}


def test_progress_is_visible_while_the_batch_runs(page):
    create(page, "D6-slow-20-rows-watch-progress.csv")

    expect(page.locator("#batch-title")).to_have_text("Creating 20 hospitals")
    expect(page.locator(".cell")).to_have_count(20)
    expect(page.locator("#batch-title")).to_have_text("20 hospitals live", timeout=10_000)


def test_batches_can_be_reopened_from_the_sidebar_and_by_link(page, base_url):
    create(page, "02-valid-3-hospitals.csv")
    expect(page.locator("#batch-title")).to_have_text("3 hospitals live", timeout=10_000)
    link = page.url

    page.click("#new-import")
    expect(page.locator("#view-import")).to_be_visible()
    page.click(".batch-item")
    expect(page.locator("#batch-title")).to_have_text("3 hospitals live")

    page.goto(base_url)
    page.goto(link)
    expect(page.locator("#batch-title")).to_have_text("3 hospitals live")


def test_clicking_a_progress_cell_highlights_its_row(page):
    create(page, "02-valid-3-hospitals.csv")
    expect(page.locator("#batch-title")).to_have_text("3 hospitals live", timeout=10_000)

    page.locator(".cell").nth(1).click()

    expect(page.locator("#row-2")).to_have_class(re.compile(r"\bis-flash\b"))


def test_directory_status_and_icons_render(page):
    expect(page.locator("#directory")).to_contain_text("Directory online")
    page.wait_for_function("document.fonts.status === 'loaded'")

    assert page.evaluate("document.fonts.check('16px Phosphor')")
    assert page.evaluate("document.fonts.check('16px \"Public Sans\"')")


def test_dark_mode_uses_dark_tokens(browser, base_url):
    context = browser.new_context(color_scheme="dark")
    page = context.new_page()
    page.goto(base_url)

    background = page.evaluate("getComputedStyle(document.body).backgroundColor")

    assert background == "rgb(13, 17, 16)"
    context.close()


@pytest.mark.parametrize("width", [375, 768])
def test_no_horizontal_scroll_on_small_screens(browser, base_url, width):
    context = browser.new_context(viewport={"width": width, "height": 800})
    page = context.new_page()
    page.goto(base_url)
    page.set_input_files("#file-input", str(SAMPLES / "D1-partial-failure-then-retry.csv"))
    page.click("#create")
    expect(page.locator("#batch-title")).to_have_text("2 hospitals failed", timeout=10_000)

    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")

    assert overflow <= 0
    context.close()
