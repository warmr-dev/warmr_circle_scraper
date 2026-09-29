"""The droplet's join walk (circle_leads/join/cdp_join.py), without a browser.

``FakeSite`` plays one Circle community: it answers the walk's page scripts by
identity and moves on when the walk clicks, types or navigates. What the tests
pin down is what cost something to learn live: the code and the profile
answers go in during the same visit, the password never reaches a host that
is not Circle, and a login stopped by a code is not recorded as a membership.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from circle_leads.join import cdp_join as w
from circle_leads.join.forms import FormResolution

EMAIL = "bot+4@example.com"
PASSWORD = "not-a-real-password"
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


class FakeSite:
    """One community, from signed out to member."""

    def __init__(self, host="demo.circle.so", *, signed_in=False, member=False,
                 code_at_login=False, code_after_join=False, questions=(), join_to_checkout=False,
                 invite_only=False, login_host=None, login_host_is_circle=False, dead=False):
        self.host = host
        self.url = "about:blank"
        self.signed_in = signed_in
        self.member = member
        self.code_at_login = code_at_login
        self.code_after_join = code_after_join
        self.questions = list(questions)       # [(id, label, field_type)]
        self.answers: dict[str, str] = {}
        self.profile_saved = not self.questions
        self.join_to_checkout = join_to_checkout
        self.invite_only = invite_only
        self.login_host = login_host or host   # where Circle shows the login form
        self.login_host_is_circle = login_host_is_circle
        self.dead = dead
        self.login_step = None                 # None | "email" | "password"
        self.on_code = False
        self.after_code = None                 # what the code unlocks
        self.expected_code = "123456"
        self.marked = None
        self.filled: list[tuple[str, str, str]] = []   # (host, selector, value)
        self.typed_codes: list[str] = []
        self.keyboard = self

    # --- Playwright surface ------------------------------------------------------
    def goto(self, url, timeout=None):
        if self.dead:
            raise RuntimeError(f"page.goto: net::ERR_NAME_NOT_RESOLVED at {url}")
        self.url = url
        self.on_code = False

    def wait_for_timeout(self, _ms):
        return None

    def wait_for_load_state(self, _state, timeout=None):
        return None

    def screenshot(self, path=None):
        return None

    def is_closed(self):
        return False

    @property
    def context(self):
        return self

    def cookies(self, _url):
        if not (self.member and self.profile_saved):
            return [{"name": "_ga", "value": "x"}]
        return [{"name": "_circle_session", "value": "s"}, {"name": "remember_user_token", "value": "r"},
                {"name": "_ga", "value": "x"}]

    def fill(self, selector, value):
        host = re.sub(r"^https?://([^/]+).*$", r"\1", self.url)
        self.filled.append((host, selector, value))
        m = re.search(r"attributes\.(\d+)\.", selector)
        if m:
            qid = self.questions[int(m.group(1))][0]
            self.answers[qid] = value

    def query_selector_all(self, selector):
        return [Box(self)] * 6 if (self.on_code and selector == w.CODE_INPUTS) else []

    def insert_text(self, text):
        self.typed_codes.append(text)
        if text == self.expected_code:
            self.on_code = False
            self.after_code()

    def click(self, selector):
        assert selector == '[data-warmr-click="1"]', selector
        label, self.marked = self.marked, None
        self._press(label)

    # --- what the page shows ---------------------------------------------------------
    def _buttons(self) -> list[str]:
        if self.on_code:
            return ["Verify"]
        if self.login_step == "email":
            return ["Continue", "Sign up"]
        if self.login_step == "password":
            return ["Log in"]
        if "/settings/profile" in self.url:
            return ["Continue"]
        if not self.signed_in:
            return ["Log in", "Join"]
        if not self.member:
            return ["Join"]
        return []

    def _press(self, label):
        if label in ("Log in", "Sign in") and self.login_step is None and not self.signed_in:
            self.login_step = "email"
            self.url = f"https://{self.login_host}/users/sign_in"
        elif label == "Continue" and self.login_step == "email":
            self.login_step = "password"
        elif label == "Log in" and self.login_step == "password":
            self.login_step = None
            if self.code_at_login:
                self.url = f"https://{self.host}/two_fa"
                self.on_code = True
                self.after_code = self._signed_in
            else:
                self._signed_in()
        elif label == "Join":
            if self.join_to_checkout:
                self.url = f"https://{self.host}/checkout/pro"
            elif self.code_after_join:
                self.on_code = True
                self.after_code = self._joined
            else:
                self._joined()
        elif label == "Continue" and "/settings/profile" in self.url:
            if all(self.answers.get(qid) for qid, _, _ in self.questions):
                self.profile_saved = True
                self.url = f"https://{self.host}/feed"

    def _signed_in(self):
        self.signed_in = True
        self.url = f"https://{self.host}/"

    def _joined(self):
        self.member = True
        if not self.profile_saved:
            self.url = f"https://{self.host}/settings/profile?new_state=true"

    def _api(self, path):
        on_circle = (self.url.split("/")[2] if "://" in self.url else "") in (self.host,) or \
            self.login_host_is_circle
        if path == "/internal_api/pundit_users":
            if not on_circle:
                return 404, "<html>not circle</html>"
            return 200, json.dumps({
                "current_community": {"id": 1},
                "current_user": {"email": EMAIL} if self.signed_in else None,
                "current_community_member": {"id": 9} if self.member else None,
            })
        if path == "/internal_api/spaces":
            if not self.member:
                return 401, json.dumps({"message": "You cannot perform this action."})
            if not self.profile_saved:
                return 400, json.dumps({"message": "Please confirm before proceeding"})
            return 200, json.dumps([{"id": 1}])
        if path == "/internal_api/signup/profile":
            return 200, json.dumps({"name": "Bot", "time_zone": "UTC", "profile_fields": [
                {"id": qid, "label": label, "field_type": kind, "required": True,
                 "community_member_profile_field": ({"text": self.answers[qid]}
                                                    if self.answers.get(qid) else None)}
                for qid, label, kind in self.questions]})
        return 404, ""

    def evaluate(self, script, arg=None):
        if script is w.FETCH_JS:
            status, body = self._api(arg)
            return {"status": status, "body": body}
        if script is w.SETTLE_JS:
            return {"ready": "complete", "len": 200, "nodes": 50, "busy": False}
        if script is w.PAGE_STATE_JS:
            buttons = [b.lower() for b in self._buttons()]
            return {
                "inviteOnlyGate": self.invite_only,
                "hasLogin": any(b in [t.lower() for t in arg["loginTexts"]] for b in buttons),
                "hasJoin": any(b in [t.lower() for t in arg["joinTexts"]] for b in buttons),
                "onCheckout": "/checkout/" in self.url,
                "subscriptionExpired": False,
                "newMemberOnboarding": "new_state=true" in self.url,
                "challenge": False,
                "url": self.url,
            }
        if script is w.POST_CLICK_JS:
            return {"pending": False, "formQuestions": [], "onCheckout": "/checkout" in self.url,
                    "newMemberOnboarding": "new_state=true" in self.url, "url": self.url}
        if script is w.MARK_CLICK_JS:
            for text in arg:
                if text in self._buttons():
                    self.marked = text
                    return text
            return None
        if script is w.FIELDS_JS:
            return {"hasEmail": self.login_step == "email",
                    "hasPassword": self.login_step == "password"}
        if script is w.CODE_PAGE_JS:
            return self.on_code
        if script is w.PROFILE_FIELD_ELEMENTS_JS:
            index = int(re.search(r"\.(\d+)\.$", arg).group(1))
            return [{"tag": "input", "type": "text", "name": f"{arg}text", "value": None,
                     "label": self.questions[index][1], "visible": True}]
        if script is w.SPACE_LINKS_JS:
            return []
        if script is w.SIGNUP_SHAPE_JS:
            return {"fillable": False, "reason": "not in this fake"}
        raise AssertionError(f"unexpected script: {script[:60]}")


class Box:
    def __init__(self, site):
        self.site = site

    def click(self):
        return None


def walk(site, *, codes=("123456",), answers=None, mailbox=True):
    fetched: list[datetime] = []

    def fetch_code(since):
        fetched.append(since)
        return codes[len(fetched) - 1] if len(fetched) <= len(codes) else None

    def answer_profile(fields):
        given = answers or {}
        return FormResolution(answers={f.id: given[f.label] for f in fields if f.label in given},
                              needs_human=[f for f in fields if f.label not in given])

    result = w.join_community(site, f"https://{site.host}/", email=EMAIL, password=PASSWORD,
                              fetch_code=fetch_code if mailbox else None,
                              answer_profile=answer_profile, now=lambda: T0)
    return result, fetched


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    # _settle() polls on a monotonic deadline; make each poll free.
    monkeypatch.setattr(w.time, "monotonic", _Clock())


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.5
        return self.t


# --- the whole way in ------------------------------------------------------------


def test_already_a_member_is_joined_with_the_hosts_session_cookies():
    site = FakeSite(signed_in=True, member=True)
    result, _ = walk(site)
    assert result.status == "joined"
    assert {c["name"] for c in result.cookies} == {"_circle_session", "remember_user_token"}
    assert {c["domain"] for c in result.cookies} == {"demo.circle.so"}


def test_signed_out_it_signs_in_types_the_mailed_code_and_joins():
    site = FakeSite(code_at_login=True)
    result, fetched = walk(site)
    assert result.status == "joined", result.detail
    assert site.typed_codes == ["123456"]
    # The code asked for is one mailed during this visit.
    assert fetched == [T0 - timedelta(seconds=5)]
    # Email first, then the password -- and only on the community's own host.
    assert [(h, v) for h, _, v in site.filled] == [("demo.circle.so", EMAIL),
                                                  ("demo.circle.so", PASSWORD)]


def test_the_join_code_and_a_profile_question_go_in_during_the_same_visit():
    site = FakeSite(signed_in=True, code_after_join=True, questions=[("q1", "Your role", "text")])
    result, fetched = walk(site, answers={"Your role": "Software engineer"})
    assert result.status == "joined", result.detail
    assert "saved the new-member profile step" in result.detail
    assert site.answers == {"q1": "Software engineer"}
    assert len(fetched) == 1


# --- where it stops ---------------------------------------------------------------


def test_no_code_at_login_is_a_login_left_waiting_not_a_membership():
    site = FakeSite(code_at_login=True)
    result, _ = walk(site, codes=())
    assert result.status == "login_code_needed"
    assert not site.member


def test_no_code_after_the_join_leaves_the_membership_to_finish():
    site = FakeSite(signed_in=True, code_after_join=True)
    result, _ = walk(site, codes=())
    assert result.status == "email_code_needed"


def test_without_a_mailbox_the_visit_says_so():
    site = FakeSite(code_at_login=True)
    result, _ = walk(site, mailbox=False)
    assert result.status == "login_code_needed"
    assert "no mailbox" in result.detail


def test_a_wrong_code_is_not_typed_again_and_again():
    site = FakeSite(code_at_login=True)
    result, fetched = walk(site, codes=("000000",))
    assert result.status == "login_code_needed"
    assert "still asks" in result.detail
    assert site.typed_codes == ["000000"]


def test_a_question_the_bot_may_not_answer_stops_with_its_label():
    site = FakeSite(signed_in=True, questions=[("q1", "Passport number", "text")])
    result, _ = walk(site, answers={})
    assert result.status == "profile_incomplete"
    assert '"Passport number"' in result.detail
    assert site.answers == {}


def test_a_login_form_on_another_site_never_gets_the_password():
    """2026-09-21: an "is this Circle?" check waved the password through to
    auth0. A host counts only when Circle's own API answers on it."""
    site = FakeSite(login_host="auth0.demo-community.com")
    result, _ = walk(site)
    assert result.status == "external_login"
    assert "auth0.demo-community.com" in result.detail
    # Not the password, and not even the email.
    assert site.filled == []


def test_a_redirect_to_the_communitys_own_domain_is_followed():
    site = FakeSite(login_host="community.demo.com", login_host_is_circle=True)
    result, _ = walk(site)
    assert result.status == "joined", result.detail
    assert ("community.demo.com", "input[type=password]", PASSWORD) in site.filled


def test_a_checkout_after_join_is_paid():
    site = FakeSite(signed_in=True, join_to_checkout=True)
    result, _ = walk(site)
    assert result.status == "paid_skip"


def test_an_invite_only_community_is_skipped():
    site = FakeSite(invite_only=True)
    result, _ = walk(site)
    assert result.status == "invite_skip"


def test_a_host_that_no_longer_resolves_is_dead():
    site = FakeSite(dead=True)
    result, _ = walk(site)
    assert result.status == "dead_host"


def test_a_page_surprise_is_a_driver_error_not_an_exception():
    class Broken(FakeSite):
        def evaluate(self, script, arg=None):
            if script is w.PAGE_STATE_JS:
                raise RuntimeError("something odd on the page")
            return super().evaluate(script, arg)

    result, _ = walk(Broken(signed_in=True))
    assert result.status == "driver_error"
    assert "something odd" in result.detail


# --- kept in step with the Ego driver ------------------------------------------------


@pytest.mark.parametrize("name", ["JOIN_TEXTS", "LOGIN_TEXTS", "EMAIL_STEP_TEXTS",
                                  "LOGIN_PIVOT_TEXTS", "PROFILE_CONTINUE_TEXTS",
                                  "SESSION_COOKIE_NAMES"])
def test_the_word_lists_match_the_ego_driver(name):
    source = (Path(w.__file__).with_name("ego_join_driver.mjs")).read_text()
    block = re.search(rf"const {name} = \[(.*?)\];", source, re.S).group(1)
    block = re.sub(r"//[^\n]*", "", block)
    assert re.findall(r'"([^"]*)"', block) == getattr(w, name)
