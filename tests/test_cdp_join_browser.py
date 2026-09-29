"""The walk's page scripts, run in a real Chromium against local pages.

tests/test_cdp_join.py answers the scripts with a fake; this makes sure each
one is valid JavaScript that returns what the walk reads. No network: the
pages are set with set_content, and the fetch script is only checked for
failing softly. Skipped where Playwright or its Chromium is not installed.
"""

from __future__ import annotations

import pytest

from circle_leads.join import cdp_join as w

sync_api = pytest.importorskip("playwright.sync_api")

LOGIN_PAGE = """<html><body><main>
  <p>Welcome to the community, a place for builders and makers of all kinds.</p>
  <a href="/c/general">General</a><a href="/c/events">Events</a>
  <button>Log in</button><a role="button">Join</a>
  <form><label>Email <input type="email" name="user[email]"></label>
        <input type="password" name="user[password]"><input type="submit" value="Continue"></form>
</main></body></html>"""

CODE_PAGE = """<html><body><h1>Check your inbox</h1><p>Enter the code we sent to your email.</p>
  <input type="tel" maxlength="1"><input type="tel" maxlength="1"><input type="tel" maxlength="1">
  <button>Verify</button></body></html>"""

PROFILE_PAGE = """<html><body><form>
  <label>Your role <input type="text" name="community_member_profile_fields_attributes.0.text"></label>
  <select name="community_member_profile_fields_attributes.1.choice">
    <option value="a">Founder</option><option value="b">Engineer</option></select>
  <label><input type="checkbox" name="community_member_profile_fields_attributes.2.flag"> Agree</label>
  <button type="button">Continue</button></form></body></html>"""


@pytest.fixture(scope="module")
def page():
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - no Chromium on this machine
            pytest.skip(f"no Chromium for Playwright: {exc}")
        page = browser.new_page()
        yield page
        browser.close()


def test_page_state_reads_login_join_and_links(page):
    page.set_content(LOGIN_PAGE)
    state = page.evaluate(w.PAGE_STATE_JS, {"joinTexts": w.JOIN_TEXTS, "loginTexts": w.LOGIN_TEXTS})
    assert state["hasLogin"] and state["hasJoin"]
    assert not state["inviteOnlyGate"] and not state["onCheckout"] and not state["challenge"]
    assert page.evaluate(w.FIELDS_JS, {"emailSelector": w.EMAIL_SELECTOR,
                                       "passwordSelector": w.PASSWORD_SELECTOR}) == {
        "hasEmail": True, "hasPassword": True}
    shape = page.evaluate(w.SIGNUP_SHAPE_JS, {"emailSelector": w.EMAIL_SELECTOR})
    assert shape["fillable"] and shape["hasEmail"]
    assert page.evaluate(w.POST_CLICK_JS)["formQuestions"] == ["Email"]
    snap = page.evaluate(w.SETTLE_JS)
    assert snap["ready"] == "complete" and snap["len"] > 40


def test_mark_click_marks_the_whole_label_only(page):
    page.set_content(LOGIN_PAGE)
    assert page.evaluate(w.MARK_CLICK_JS, ["Join space", "Join"]) == "Join"
    assert page.get_attribute('[data-warmr-click="1"]', "role") == "button"
    assert page.evaluate(w.MARK_CLICK_JS, ["No such button"]) is None
    assert page.query_selector('[data-warmr-click="1"]') is None


def test_the_code_page_is_recognised_and_takes_the_code(page):
    page.set_content(CODE_PAGE)
    assert page.evaluate(w.CODE_PAGE_JS, w.CODE_INPUTS) is True
    assert len(page.query_selector_all(w.CODE_INPUTS)) == 3
    page.set_content(LOGIN_PAGE)
    assert page.evaluate(w.CODE_PAGE_JS, w.CODE_INPUTS) is False


def test_profile_fields_are_found_by_their_index(page):
    page.set_content(PROFILE_PAGE)
    prefix = "community_member_profile_fields_attributes."
    text = page.evaluate(w.PROFILE_FIELD_ELEMENTS_JS, prefix + "0.")
    assert [(e["tag"], e["type"], e["visible"]) for e in text] == [("input", "text", True)]
    select = page.evaluate(w.PROFILE_FIELD_ELEMENTS_JS, prefix + "1.")
    page.select_option(f'[name="{select[0]["name"]}"]', "Engineer")
    assert page.evaluate(w.SELECTED_JS, select[0]["name"]) == ["Engineer"]
    box = page.evaluate(w.PROFILE_FIELD_ELEMENTS_JS, prefix + "2.")[0]
    assert page.evaluate(w.CHECKED_JS, box["name"]) is False


def test_space_links_and_a_failed_fetch(page):
    page.set_content(LOGIN_PAGE)
    assert page.evaluate(w.SPACE_LINKS_JS) == []  # about:blank has no origin to match
    result = page.evaluate(w.FETCH_JS, "/internal_api/pundit_users")
    assert result["status"] == 0 and result["body"] == ""
