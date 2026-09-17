import pytest

from circle_leads.discovery.discover_communities import (
    communities_from_urls,
    community_slug_for_host,
    dedupe,
    detect_platform,
    extract_from_text,
    is_circle_platform,
    load_from_file,
    normalize_community_url,
    platform_from_host,
    unique_slug_for_host,
)
from circle_leads.discovery.validate_community import assess_relevance


@pytest.mark.parametrize(
    "host,expected",
    [
        ("foo.circle.so", "circle"),
        ("startup-founders.circle.so", "circle"),
        ("discover.circle.so", "circle_infra"),
        ("app.circle.so", "circle_infra"),
        ("login.circle.so", "circle_infra"),
        ("circle.so", "circle_infra"),
        ("myworkspace.slack.com", "slack"),
        ("join.slack.com", "slack"),
        ("www.facebook.com", "facebook"),
        ("facebook.com", "facebook"),
        ("some-group.skool.com", "skool"),
        ("acme.mn.co", "mighty_networks"),
        ("discord.gg", "discord"),
        # a bare custom domain can't be judged by name alone
        ("forum.joelpilger.com", None),
        ("members.skl.club", None),
    ],
)
def test_platform_from_host(host, expected):
    assert platform_from_host(host) == expected


def test_is_circle_platform():
    assert is_circle_platform("circle") is True
    assert is_circle_platform("circle_infra") is False
    assert is_circle_platform("facebook") is False
    assert is_circle_platform(None) is False


def test_detect_platform_uses_host_heuristics_without_network():
    # These never touch the network (host is decisive).
    assert detect_platform("https://foo.circle.so") == "circle"
    assert detect_platform("https://team.slack.com/join") == "slack"
    assert detect_platform("https://discover.circle.so/products/x") == "discover"
    assert detect_platform("manual://flutter-devs") == "other"


@pytest.mark.parametrize(
    "host,expected",
    [
        ("foo.circle.so", "foo"),
        ("startup-founders.circle.so", "startup-founders"),
        # custom domains: the collision cases that motivated this helper
        ("www.siliconslopes.com", "siliconslopes"),
        ("www.yourspinstate.com", "yourspinstate"),
        ("community.bigstarlights.com", "bigstarlights"),
        ("siliconslopes.com", "siliconslopes"),
        ("portal.acme.io", "acme"),
        # two-part public suffixes
        ("example.co.uk", "example"),
        ("www.example.co.uk", "example"),
        ("", "unknown"),
    ],
)
def test_community_slug_for_host(host, expected):
    assert community_slug_for_host(host) == expected


def test_slug_helper_does_not_collapse_unrelated_www_hosts():
    a = community_slug_for_host("www.siliconslopes.com")
    b = community_slug_for_host("www.yourspinstate.com")
    assert a != b  # the old host.split(".")[0] made both "www"


@pytest.mark.parametrize(
    "raw,expected_slug",
    [
        ("https://startup-founders.circle.so", "startup-founders"),
        ("startup-founders.circle.so", "startup-founders"),
        ("https://devs.circle.so/c/general/post-123", "devs"),
        ("HTTPS://Devs.Circle.SO/", "devs"),
    ],
)
def test_normalize_valid_urls(raw, expected_slug):
    result = normalize_community_url(raw)
    assert result is not None
    assert result[0] == expected_slug


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com",
        "https://app.circle.so/settings",   # reserved infrastructure host
        "https://discover.circle.so",       # directory, not a community
        "https://circle.so/pricing",
        "",
        "not a url",
    ],
)
def test_normalize_rejects_non_communities(raw):
    assert normalize_community_url(raw) is None


def test_extract_finds_communities_in_public_html():
    html = """
      <a href="https://startup-founders.circle.so">Founders</a>
      <a href="https://saas-builders.circle.so/c/jobs">SaaS Builders</a>
      <a href="https://app.circle.so/login">Login</a>
      <a href="https://unrelated.com">Other</a>
    """
    found = extract_from_text(html, source="test")
    slugs = sorted(c.slug for c in found)
    assert slugs == ["saas-builders", "startup-founders"]


def test_extract_deduplicates_repeated_links():
    html = "a.circle.so a.circle.so/c/x https://a.circle.so/posts/1"
    assert len(extract_from_text(html)) == 1


def test_dedupe_merges_metadata():
    from circle_leads.discovery.discover_communities import DiscoveredCommunity

    merged = dedupe([
        DiscoveredCommunity(slug="a", url="https://a.circle.so", name="A"),
        DiscoveredCommunity(slug="a", url="https://a.circle.so", description="desc"),
    ])
    assert len(merged) == 1
    assert merged[0].name == "A"
    assert merged[0].description == "desc"


def test_business_community_is_relevant():
    a = assess_relevance("Startup Founders", "A community for SaaS founders and entrepreneurs building tech products")
    assert a.relevant
    assert "startup" in a.reasons


def test_hobby_community_is_not_relevant():
    a = assess_relevance("Knitting Circle", "Share knitting patterns and recipes with fellow crafters")
    assert not a.relevant


def test_hiring_space_boosts_relevance():
    with_jobs = assess_relevance("Dev Community", "Software developers. Jobs board and hiring channel.")
    without = assess_relevance("Dev Community", "Software developers chatting.")
    assert with_jobs.score > without.score


def test_empty_metadata_scores_zero():
    assert assess_relevance(None, None).score == 0


# --- Bare hostnames without a scheme ---------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("visit foo.circle.so", ["foo"]),
        ("baz.circle.so/c/jobs", ["baz"]),
        ("Join qux.circle.so!", ["qux"]),
        ("(quux.circle.so)", ["quux"]),
        ("Community: my-startup.circle.so.", ["my-startup"]),
        ("http://legacy.circle.so", ["legacy"]),
    ],
)
def test_extracts_bare_hostnames_without_a_scheme(text, expected):
    """People write "foo.circle.so" in prose far more often than a full URL."""
    assert [c.slug for c in extract_from_text(text)] == expected


@pytest.mark.parametrize(
    "text",
    [
        "dana@app.circle.so",       # email domain, not a link
        "sub.domain.circle.so",     # deeper hostname, not a community slug
        "discover.circle.so",       # directory, reserved
        "notcircle.so",
        "xcircle.so",
    ],
)
def test_bare_hostname_matching_does_not_over_capture(text):
    assert extract_from_text(text) == []


# --- Custom-domain intake (opt-in) -----------------------------------------
# 62% of directory-resolved Circle communities live on their own domain, and
# they are where the ICP-fit rows and the successful joins actually come from.
# The suffix check that used to gate intake could not see any of them.


@pytest.mark.parametrize(
    "raw,expected_slug",
    [
        # The slug is the host: it is a key, and a registrable label ("acme")
        # is shared by acme.com, acme.io and acme.co.uk.
        ("https://community.bigstarlights.com", "community.bigstarlights.com"),
        ("community.bigstarlights.com", "community.bigstarlights.com"),
        ("https://www.siliconslopes.com/c/jobs", "www.siliconslopes.com"),
        ("HTTPS://Forum.JoelPilger.com/", "forum.joelpilger.com"),
        ("https://members.skl.club", "members.skl.club"),
    ],
)
def test_custom_domains_are_accepted_under_the_opt_in(raw, expected_slug):
    assert normalize_community_url(raw) is None          # default: still rejected
    result = normalize_community_url(raw, allow_custom_domains=True)
    assert result is not None
    assert result[0] == expected_slug


def test_opt_in_canonicalizes_to_the_bare_host():
    slug, url = normalize_community_url(
        "https://Community.Acme.io/c/general/post-7", allow_custom_domains=True
    )
    assert (slug, url) == ("community.acme.io", "https://community.acme.io")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not a url",
        "https://localhost:3000",       # no public suffix
        "https://192.168.1.10",         # IP literal, not a community host
        "https://app.circle.so/settings",   # circle infrastructure, still reserved
        "https://discover.circle.so",
        "https://circle.so/pricing",
        "https://sub.domain.circle.so",     # deeper host, not a community slug
        "https://myworkspace.slack.com",    # named platform: no probe will help
        "https://www.facebook.com/groups/1",
        "https://some-group.skool.com",
        "https://acme.mn.co",
    ],
)
def test_opt_in_still_rejects_what_is_not_a_community_host(raw):
    assert normalize_community_url(raw, allow_custom_domains=True) is None


def test_opt_in_does_not_collapse_unrelated_custom_hosts():
    """commit f741b80: host.split(".")[0] put every www.* host on one row."""
    a = normalize_community_url("https://www.siliconslopes.com", allow_custom_domains=True)
    b = normalize_community_url("https://www.yourspinstate.com", allow_custom_domains=True)
    c = normalize_community_url("https://community.bigstarlights.com", allow_custom_domains=True)
    assert len({a[0], b[0], c[0]}) == 3
    assert len({a[1], b[1], c[1]}) == 3


@pytest.mark.parametrize(
    "raw,expected_slug",
    [
        ("https://startup-founders.circle.so", "startup-founders"),
        ("devs.circle.so/c/general/post-123", "devs"),
    ],
)
def test_circle_so_behaviour_is_unchanged_by_the_opt_in(raw, expected_slug):
    assert normalize_community_url(raw) == normalize_community_url(
        raw, allow_custom_domains=True
    )
    assert normalize_community_url(raw)[0] == expected_slug


def test_extract_picks_up_custom_domains_only_under_the_opt_in():
    html = """
      <a href="https://startup-founders.circle.so">Founders</a>
      <a href="https://community.bigstarlights.com/join">Big Star</a>
      <a href="https://app.circle.so/login">Login</a>
      <a href="https://www.facebook.com/groups/42">FB</a>
    """
    assert sorted(c.slug for c in extract_from_text(html)) == ["startup-founders"]
    assert sorted(c.slug for c in extract_from_text(html, allow_custom_domains=True)) == [
        "community.bigstarlights.com", "startup-founders",
    ]


def test_extract_ignores_bare_custom_hostnames_in_prose():
    """Prose names domains constantly; only a written-out link is intentional."""
    text = "We moved off notion.so and mightynetworks.com last year."
    assert extract_from_text(text, allow_custom_domains=True) == []


def test_communities_from_urls_takes_bare_operator_supplied_hosts():
    from circle_leads.discovery.discover_communities import communities_from_urls

    urls = ["community.bigstarlights.com", "startup-founders.circle.so",
            "https://www.facebook.com/groups/42"]
    assert sorted(c.slug for c in communities_from_urls(urls)) == ["startup-founders"]
    found = communities_from_urls(urls, allow_custom_domains=True, source="cli")
    assert sorted(c.slug for c in found) == [
        "community.bigstarlights.com", "startup-founders"]
    assert {c.source for c in found} == {"cli"}


def test_communities_from_urls_deduplicates():
    from circle_leads.discovery.discover_communities import communities_from_urls

    found = communities_from_urls(
        ["https://community.acme.io", "community.acme.io/c/jobs"],
        allow_custom_domains=True,
    )
    assert len(found) == 1


def test_load_from_file_honours_the_opt_in(tmp_path):
    from circle_leads.discovery.discover_communities import load_from_file

    listing = tmp_path / "communities.txt"
    listing.write_text(
        "# hand-curated\n"
        "https://startup-founders.circle.so\n"
        "community.bigstarlights.com\n",
        encoding="utf-8",
    )
    assert [c.slug for c in load_from_file(listing)] == ["startup-founders"]
    assert [c.slug for c in load_from_file(listing, allow_custom_domains=True)] == [
        "startup-founders", "community.bigstarlights.com",
    ]


def test_load_from_csv_honours_the_opt_in(tmp_path):
    from circle_leads.discovery.discover_communities import load_from_file

    listing = tmp_path / "communities.csv"
    listing.write_text(
        "url,name\nhttps://community.bigstarlights.com,Big Star\n", encoding="utf-8"
    )
    assert load_from_file(listing) == []
    rows = load_from_file(listing, allow_custom_domains=True)
    assert [(c.slug, c.name) for c in rows] == [
        ("community.bigstarlights.com", "Big Star")]


def test_custom_domain_platform_judgement_is_left_to_detect_platform():
    """Intake must not pretend to know; detect_platform() makes the call."""
    slug, url = normalize_community_url(
        "https://forum.joelpilger.com", allow_custom_domains=True
    )
    assert platform_from_host("forum.joelpilger.com") is None

    class _Resp:
        headers = {"content-type": "application/json; charset=utf-8"}
        text = ""

    class _Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append(url)
            return _Resp()

    session = _Session()
    assert detect_platform(url, session=session) == "circle"
    assert session.calls == ["https://forum.joelpilger.com/internal_api/spaces"]


@pytest.mark.parametrize(
    "host,expected",
    [
        # Front labels nobody can enumerate: the slug is read off the suffix
        # end, so these do not all collapse onto one row.
        ("forum.joelpilger.com", "joelpilger"),
        ("learn.acme.com", "acme"),
        ("school.bigstarlights.com", "bigstarlights"),
        ("hub.members.acme.io", "acme"),
        # two-part public suffixes still resolve to the registrable label
        ("forum.example.co.uk", "example"),
        ("members.example.com.au", "example"),
    ],
)
def test_slug_uses_the_registrable_label_not_the_first_one(host, expected):
    assert community_slug_for_host(host) == expected


def test_unlisted_front_labels_do_not_collide():
    """`forum.a.com` and `forum.b.com` are different communities."""
    slugs = {
        community_slug_for_host("forum.joelpilger.com"),
        community_slug_for_host("forum.siliconslopes.com"),
        community_slug_for_host("learn.yourspinstate.com"),
    }
    assert len(slugs) == 3


# --- Custom-domain slugs are keys, so they must be unique per host -----------
# `community_slug_for_host` answers "which organisation?" and is intentionally
# not unique. Discovery keys on the value it stores, and `get_or_create_community`
# matches on `slug OR url`, so a shared slug merged unrelated communities into
# one row -- and the in-memory dedupe lost them even before the DB saw them.


def test_unique_slug_keeps_same_label_different_suffix_apart():
    slugs = {
        unique_slug_for_host("acme.com"),
        unique_slug_for_host("acme.io"),
        unique_slug_for_host("acme.co.uk"),
    }
    assert len(slugs) == 3  # community_slug_for_host returns "acme" for all three


def test_unique_slug_keeps_sibling_subdomains_apart():
    assert unique_slug_for_host("forum.bravo.com") != unique_slug_for_host("members.bravo.com")


@pytest.mark.parametrize(
    "host,expected",
    [
        ("foo.circle.so", "foo"),
        ("startup-founders.circle.so", "startup-founders"),
        ("FOO.CIRCLE.SO", "foo"),
        ("", "unknown"),
    ],
)
def test_unique_slug_leaves_circle_so_semantics_untouched(host, expected):
    """.circle.so keeps its historic slug byte-for-byte, so old rows still match."""
    assert unique_slug_for_host(host) == expected


def test_unique_slug_cannot_collide_with_a_circle_so_slug():
    """A custom-domain slug always contains a dot; a .circle.so slug never can."""
    assert "." in unique_slug_for_host("acme.com")
    assert "." not in unique_slug_for_host("acme.circle.so")


def test_normalize_custom_domain_slug_is_unique_per_host():
    hosts = ["acme.com", "acme.io", "acme.co.uk", "forum.bravo.com", "members.bravo.com"]
    slugs = {
        normalize_community_url(h, allow_custom_domains=True)[0] for h in hosts
    }
    assert len(slugs) == len(hosts)


def test_normalize_circle_so_result_is_unchanged_under_the_opt_in():
    assert normalize_community_url("https://devs.circle.so/c/general/post-1",
                                   allow_custom_domains=True) == ("devs", "https://devs.circle.so")


def test_dedupe_keeps_unrelated_custom_domains():
    """The reviewer's case: three `acme` labels are three communities."""
    from circle_leads.discovery.discover_communities import DiscoveredCommunity

    rows = [
        DiscoveredCommunity(*normalize_community_url(h, allow_custom_domains=True))
        for h in ("acme.com", "acme.io", "acme.co.uk",
                  "forum.bravo.com", "members.bravo.com", "charlie.circle.so")
    ]
    kept = dedupe(rows)
    assert len(kept) == 6
    assert len({c.url for c in kept}) == 6


def test_dedupe_still_collapses_one_host_written_several_ways():
    """Same host, different scheme/case/trailing slash -> one row, fields merged."""
    from circle_leads.discovery.discover_communities import DiscoveredCommunity

    rows = [
        DiscoveredCommunity(*normalize_community_url(raw, allow_custom_domains=True),
                            **extra)
        for raw, extra in (
            ("http://forum.bravo.com", {"name": "Bravo"}),
            ("https://forum.bravo.com/", {"description": "desc"}),
            ("FORUM.BRAVO.COM", {"price_label": "Free"}),
        )
    ]
    merged = dedupe(rows)
    assert len(merged) == 1
    assert (merged[0].name, merged[0].description, merged[0].price_label) == (
        "Bravo", "desc", "Free")


def test_communities_from_urls_keeps_every_custom_domain():
    found = communities_from_urls(
        ["acme.com", "acme.io", "acme.co.uk", "forum.bravo.com",
         "members.bravo.com", "charlie.circle.so"],
        allow_custom_domains=True,
    )
    assert len(found) == 6
    assert len({c.slug for c in found}) == 6


def test_communities_from_urls_collapses_one_host_written_several_ways():
    found = communities_from_urls(
        ["http://forum.bravo.com", "https://forum.bravo.com/", "FORUM.BRAVO.COM"],
        allow_custom_domains=True,
    )
    assert len(found) == 1


def test_extract_from_text_keeps_same_label_different_suffix():
    html = '<a href="https://acme.com">A</a> <a href="https://acme.io/join">B</a>'
    found = extract_from_text(html, allow_custom_domains=True)
    assert sorted(c.url for c in found) == ["https://acme.com", "https://acme.io"]


def test_extract_from_text_still_collapses_repeats_of_one_host():
    html = ('<a href="http://forum.bravo.com">x</a> '
            '<a href="https://forum.bravo.com/c/general">y</a>')
    assert len(extract_from_text(html, allow_custom_domains=True)) == 1


def test_load_from_file_then_dedupe_loses_nothing(tmp_path):
    """End to end on the reported input: six hosts in, six communities out."""
    listing = tmp_path / "communities.txt"
    listing.write_text(
        "acme.com\nacme.io\nacme.co.uk\nforum.bravo.com\n"
        "members.bravo.com\ncharlie.circle.so\n",
        encoding="utf-8",
    )
    loaded = load_from_file(listing, allow_custom_domains=True)
    assert len(loaded) == 6
    kept = dedupe(loaded)
    assert len(kept) == 6
    assert len({c.slug for c in kept}) == 6


def test_discover_records_every_custom_domain(tmp_path):
    """The loss was only visible in the DB, so assert there too (sqlite, no network)."""
    from sqlalchemy import select

    from circle_leads.pipeline import discover
    from circle_leads.storage.database import Database
    from circle_leads.storage.models import Community

    db = Database(f"sqlite:///{tmp_path}/discover.db")
    hosts = ["acme.com", "acme.io", "acme.co.uk", "forum.bravo.com",
             "members.bravo.com", "charlie.circle.so"]
    communities = dedupe(communities_from_urls(hosts, allow_custom_domains=True))
    discover(db, communities, validate=False)
    with db.session() as s:
        rows = s.scalars(select(Community)).all()
        assert len(rows) == 6
        assert len({r.url for r in rows}) == 6


# --- A slug is a key, so it must not merge two communities -------------------


def test_sibling_subdomains_persist_as_two_communities(tmp_path):
    """forum.acme.com and shop.acme.com are two communities, not one.

    community_slug_for_host answers "which organisation is this?" and is
    deliberately NOT unique -- both those hosts reduce to "acme". Because
    get_or_create_community matches on ``slug OR url``, feeding it that label
    merges the second community onto the first row and throws its URL away,
    with no error. Every caller that persists must use unique_slug_for_host.
    Custom domains are 2% of prod rows but 31% of successful joins, so this
    loss would be both silent and expensive.
    """
    from sqlalchemy import func, select

    from circle_leads.discovery.discover_communities import unique_slug_for_host
    from circle_leads.storage.database import Database, get_or_create_community
    from circle_leads.storage.models import Community

    db = Database(f"sqlite:///{tmp_path}/siblings.db")
    hosts = ["forum.acme.com", "shop.acme.com", "acme.com"]
    with db.session() as s:
        for host in hosts:
            get_or_create_community(
                s, slug=unique_slug_for_host(host), url=f"https://{host}"
            )
        s.flush()
        assert s.scalar(select(func.count()).select_from(Community)) == len(hosts)
        stored = {c.url for c in s.scalars(select(Community)).all()}
    assert stored == {f"https://{h}" for h in hosts}


def test_the_persisting_callers_do_not_use_the_label_slug():
    """Guard the fix at its call sites, not just at the helper.

    The helper was correct and the callers were not; a test that only exercises
    unique_slug_for_host would stay green while the operator-facing "add by
    link" handler kept merging rows. community_intake.py is deliberately
    excluded: its slug is a key in an EXTERNAL system, and 15 of the 26 live
    connection hosts would change identity if it were switched.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "circle_leads"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name in ("discover_communities.py", "community_intake.py"):
            continue
        if "community_slug_for_host(" in path.read_text():
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"these persist with the non-unique label slug: {offenders}"
