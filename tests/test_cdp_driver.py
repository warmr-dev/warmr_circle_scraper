"""The deterministic half of a join: classification, the credential host guard,
and the two input quirks that cost us real money to discover (React fields, the
six-box OTP). No browser -- ``FakePage`` answers like one and records what was
typed, so the tests can assert what the driver refuses to do.
"""

from __future__ import annotations

import pytest

from circle_leads.join import cdp_driver as d


class FakePage:
    def __init__(self, *, url="https://community.example.com/", body="", buttons=(), inputs=()):
        self.url = url
        self._body = body
        self._buttons = list(buttons)
        self._inputs = {name: FakeElement(name, self) for name in inputs}
        self.typed: list[tuple[str, str]] = []
        self.clicked: list[str] = []
        self.keyboard = FakeKeyboard(self)

    # --- page API used by the driver -------------------------------------
    def inner_text(self, _selector):
        return self._body

    def eval_on_selector_all(self, _selector, _script):
        return [b.lower() for b in self._buttons]

    def query_selector(self, selector):
        for name, element in self._inputs.items():
            if name in selector:
                return element
        for label in self._buttons:
            if label.lower() in selector.lower():
                self.clicked.append(label)
                return FakeElement(label, self)
        return None

    def query_selector_all(self, selector):
        if "tel" in selector:
            return [FakeElement(f"otp{i}", self) for i in range(6)]
        return []

    def evaluate(self, _script):
        return self.spaces_response

    def wait_for_selector(self, selector, timeout=None):
        if selector not in self._inputs:
            raise TimeoutError(selector)
        return self._inputs[selector]

    def wait_for_timeout(self, _ms):
        return None

    def wait_for_load_state(self, _state, timeout=None):
        return None


class FakeElement:
    def __init__(self, name, page):
        self.name = name
        self.page = page

    def click(self):
        self.page.clicked.append(self.name)
        self.page.focused = self.name


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def insert_text(self, text):
        self.page.typed.append((getattr(self.page, "focused", "?"), text))

    def press(self, key):
        self.page.clicked.append(f"key:{key}")


# --- the credential host guard ------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://community.thefpahub.com/users/sign_in", True),
        ("https://login.circle.so/sign_in?request_host=go.b2btactics.com", True),
        ("https://b2b-tactics.circle.so/", True),
        ("https://www.community.thefpahub.com/", True),
        ("https://auth0.aimarketerhq.com/login", False),
        ("https://login.ecommerce.pl/", False),
        ("https://circle.so.evil.example/", False),
        ("", False),
    ],
)
def test_credentials_only_on_the_community_host_or_circle(url, expected):
    """The 2026-09-21 leak went to an auth0 page that merely *looked* related."""
    assert d.is_circle_host(url, "community.thefpahub.com") is expected


def test_sign_in_refuses_a_foreign_login_page():
    page = FakePage(url="https://login.ecommerce.pl/", inputs=("email",))
    state = d.sign_in(page, email="bot@example.com", password="secret", community_host="ecommerce.pl")
    assert state == d.EXTERNAL_LOGIN
    assert page.typed == []  # nothing was typed anywhere


# --- classification ------------------------------------------------------


def test_two_fa_is_recognised_by_url():
    assert d.classify(FakePage(url="https://x.circle.so/two_fa#email")) == d.TWO_FA


def test_profile_step_is_recognised_by_url():
    page = FakePage(url="https://x.circle.so/settings/profile?new_state=true")
    assert d.classify(page) == d.PROFILE_STEP


def test_members_only_page_with_a_join_button_is_a_join_gate():
    page = FakePage(body="Members Only Community. Please login or sign up.", buttons=["Register for Free"])
    assert d.classify(page) == d.JOIN_GATE


def test_members_only_page_without_a_join_button_is_a_login_wall():
    page = FakePage(body="Members only community", buttons=["Log in"])
    assert d.classify(page) == d.LOGIN_WALL


def test_cloudflare_interstitial_is_not_mistaken_for_a_login_wall():
    page = FakePage(body="Just a moment... checking your browser", buttons=["Log in"])
    assert d.classify(page) == d.CLOUDFLARE


def test_a_page_we_do_not_recognise_stays_unknown():
    """UNKNOWN is what routes a community to the agent, so it must not be guessed."""
    page = FakePage(body="Welcome to our blog", buttons=["Subscribe"])
    assert d.classify(page) == d.UNKNOWN


# --- membership ----------------------------------------------------------


def test_one_true_flag_means_member():
    page = FakePage()
    page.spaces_response = {"count": 10, "flags": [True] + [None] * 9}
    m = d.membership(page)
    assert m.is_member and m.count == 10


def test_all_null_flags_mean_signed_in_but_not_a_member():
    page = FakePage()
    page.spaces_response = {"count": 2, "flags": [None, None]}
    assert d.membership(page).is_member is False


def test_a_fetch_error_is_reported_not_swallowed():
    page = FakePage()
    page.spaces_response = {"error": "TypeError: failed to fetch"}
    m = d.membership(page)
    assert m.is_member is False and "failed to fetch" in m.error


# --- the OTP boxes -------------------------------------------------------


def test_code_is_inserted_into_the_first_box_in_one_go():
    """Six boxes, one insert: Circle's own handler distributes the digits and
    auto-submits. Filling them one by one from JS registers nothing."""
    page = FakePage(url="https://x.circle.so/two_fa")
    d.enter_code(page, "123456")
    assert page.typed == [("otp0", "123456")]


def test_a_malformed_code_is_refused_before_touching_the_page():
    page = FakePage(url="https://x.circle.so/two_fa")
    with pytest.raises(d.DriverError, match="6-digit"):
        d.enter_code(page, "12ab")
    assert page.typed == []


# --- membership beats the look of the page -------------------------------


def test_a_community_we_already_joined_is_MEMBER_even_with_a_join_button():
    """Measured live: b2b-tactics still renders "Join" to a member. Without
    this precedence every joined community would be sent to the agent again."""
    page = FakePage(body="Members Only Community", buttons=["Join"])
    page.spaces_response = {"count": 10, "flags": [True] + [None] * 9}
    state, member = d.assess(page)
    assert state == d.MEMBER and member.is_member


def test_a_member_page_with_no_buttons_is_MEMBER_not_UNKNOWN():
    page = FakePage(body="Latest posts", buttons=[])
    page.spaces_response = {"count": 10, "flags": [True] * 10}
    assert d.assess(page)[0] == d.MEMBER


def test_a_non_member_still_gets_the_dom_verdict():
    page = FakePage(body="Members Only Community", buttons=["Register for Free"])
    page.spaces_response = {"count": 2, "flags": [None, None]}
    assert d.assess(page)[0] == d.JOIN_GATE


def test_sign_in_opens_the_login_page_when_the_current_one_has_no_field():
    """A community home page greets a visitor with a join gate and no form;
    Circle keeps the form at /users/sign_in (measured on generouslifeapp)."""
    page = FakePage(url="https://community.example.com/", body="Members Only")

    visited = []
    def goto(url, wait_until=None):
        visited.append(url)
        page._inputs["email"] = FakeElement("email", page)  # the form exists there
        page.url = url
    page.goto = goto

    d.sign_in(page, email="bot@example.com", password="pw", community_host="community.example.com")
    assert visited == ["https://community.example.com/users/sign_in"]
    assert ("email", "bot@example.com") in page.typed
