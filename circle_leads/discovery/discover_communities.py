"""Phase 1: discover candidate communities from legitimate public sources.

Sources are limited to: user-supplied lists, public directory pages, and links
found on public pages. Subdomain brute-forcing is deliberately not implemented
-- it is guessing at private infrastructure, not discovery.

Discovery records public listing metadata only. It never implies permission to
ingest a community's conversations; that requires operator approval recorded
separately in a permission file.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# The scheme is optional: people write "foo.circle.so" in prose, bios and
# forum posts far more often than the full URL. A preceding word character,
# "@" or "." is excluded so email domains and deeper hostnames
# (a.b.circle.so) are not mistaken for a community slug.
CIRCLE_HOST_RX = re.compile(
    r"(?<![\w@.])(?:https?://)?([a-z0-9][a-z0-9-]{0,62})\.circle\.so(?:/[^\s\"'<>)]*)?",
    re.I,
)
# Any http(s) link, for the custom-domain opt-in. Scheme required: see
# extract_from_text for why a bare-hostname sweep over prose is not safe.
_EXPLICIT_URL_RX = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)

# Circle's own hosts are infrastructure, not member communities.
RESERVED_SLUGS = {
    "app", "www", "api", "help", "discover", "status", "docs", "developers",
    "blog", "support", "admin", "assets", "cdn", "api-headless", "marketing",
    "login", "community", "signup", "sign-in", "auth", "static", "media",
    "email", "mail", "go", "link", "links", "share", "embed", "webhooks",
    "circle-compass-assets", "compass", "brand", "partners", "build-summit",
}


@dataclass
class DiscoveredCommunity:
    slug: str
    url: str
    name: str | None = None
    description: str | None = None
    price_label: str | None = None
    source: str = "unknown"
    metadata: dict = field(default_factory=dict)


# Leading host labels that identify infrastructure, not the community itself.
# Stripped before deriving a slug so `www.acme.com` and `community.acme.com`
# don't collapse to different first labels for the same organisation.
_INFRA_HOST_LABELS = {
    "www", "app", "apps", "community", "communities", "portal", "hub", "go",
    "my", "members", "member", "login", "get", "join", "circle", "space",
}


# Public suffixes that are two labels long. Only these may be treated as a
# suffix *pair*; everything else has a single-label TLD. The list is short on
# purpose (no PSL dependency) -- a miss costs a slightly odd *label*, which is
# all this is used for. It is not used as a key: see `unique_slug_for_host` for
# why a registrable label can never be one.
_TWO_PART_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "co.nz", "org.nz",
    "co.za", "co.il", "co.in", "co.kr", "co.jp", "ne.jp", "or.jp",
    "com.au", "net.au", "org.au", "com.br", "com.mx", "com.ar", "com.co",
    "com.sg", "com.tr", "com.cn", "com.hk", "com.tw", "com.pl", "com.ua",
}


def community_slug_for_host(host: str) -> str:
    """A stable, collision-resistant community slug for any host.

    - `foo.circle.so`            -> `foo`   (the subdomain label)
    - `www.siliconslopes.com`    -> `siliconslopes`
    - `community.bigstarlights.com` -> `bigstarlights`
    - `forum.joelpilger.com`     -> `joelpilger`
    - `siliconslopes.com`        -> `siliconslopes`
    - `example.co.uk`            -> `example`

    The old `host.split(".")[0]` collapsed every `www.*` host to `www` and
    every `community.*` host to `community`, so unrelated communities landed on
    one row. The registrable label -- the one right before the public suffix --
    is what identifies the organisation, so that is the slug.

    Stripping a leading infrastructure label is not enough on its own: a host
    like `forum.acme.com` keeps a label nobody listed, and reading the slug off
    the *front* of the host brings the same collision back (every `forum.*`
    community on one row). Counting in from the suffix end avoids needing a
    complete list of front labels.
    """
    host = (host or "").strip().lower().strip(".")
    if not host:
        return "unknown"
    if host.endswith(".circle.so"):
        return host[: -len(".circle.so")].split(".")[-1] or "circle"
    labels = host.split(".")
    if len(labels) <= 2:
        return labels[0]
    if labels[0] in _INFRA_HOST_LABELS:
        labels = labels[1:]
    if len(labels) <= 2:
        return labels[0]
    if ".".join(labels[-2:]) in _TWO_PART_SUFFIXES:
        return labels[-3]
    return labels[-2]


def unique_slug_for_host(host: str) -> str:
    """A slug that identifies exactly one host -- the key custom domains need.

    ``community_slug_for_host`` answers a different question ("which
    organisation is this host?") and its answer is deliberately *not* unique:
    `acme.com`, `acme.io` and `acme.co.uk` all reduce to `acme`, and so do
    `forum.bravo.com` and `members.bravo.com`. That is right for a label and
    fatal for a key, because `get_or_create_community` matches on
    ``slug OR url``: a shared slug merges unrelated communities onto one row
    before the unique URL ever gets a say. The in-memory dedupe collapsed them
    even earlier, so an import of six custom domains landed as three.

    So on a custom domain the slug *is* the host. It reads fine
    (`forum.bravo.com`), it cannot collide, and it cannot collide with a
    ``.circle.so`` slug either: those are a single subdomain label and never
    contain a dot.

    ``.circle.so`` keeps its historic slug byte-for-byte (the subdomain), so
    every existing row still matches.
    """
    host = (host or "").strip().lower().strip(".")
    if not host:
        return "unknown"
    if host.endswith(".circle.so"):
        return community_slug_for_host(host)
    return host


def dedupe_key(url: str, slug: str = "") -> str:
    """The value two discoveries must share to be the same community.

    The host, never the slug. Hosts are what communities actually are; slugs
    are derived and (for a registrable label) shared by unrelated orgs. Using
    the host also collapses the harmless differences -- scheme, case, a
    trailing slash, a deep path -- that make one community look like several.

    URLs with no host (`manual://…` handles, a bare identifier) fall back to
    the whole string, then to the slug, so nothing is silently merged.
    """
    raw = (url or "").strip()
    if raw:
        try:
            host = (urlparse(raw).hostname or "").lower().strip(".")
        except ValueError:
            host = ""
        if host:
            return host
        return raw.lower().rstrip("/")
    return (slug or "").strip().lower()


# --- Platform classification --------------------------------------------------
# The Circle Discover directory (and web search) turn up "communities" that are
# not on Circle at all -- Facebook groups, Slack workspaces, Skool, Mighty
# Networks. We can't read those, but throwing the find away loses the research.
# Instead every community row carries a `platform`, and the harvest reads only
# the ones on Circle (subdomain OR custom domain).

PLATFORM_CIRCLE = "circle"
PLATFORM_DISCOVER = "discover"          # a discover.circle.so listing, host unknown
PLATFORM_CIRCLE_INFRA = "circle_infra"  # app/login/... .circle.so, not a community
PLATFORM_OTHER = "other"                # reachable, but not a Circle community

# host suffix -> platform. Checked with endswith(), so it also matches subdomains
# (foo.slack.com, bar.mn.co).
_PLATFORM_SUFFIXES = {
    ".slack.com": "slack",
    ".skool.com": "skool",
    ".mn.co": "mighty_networks",
    ".mightynetworks.com": "mighty_networks",
    ".facebook.com": "facebook",
    ".discord.com": "discord",
    ".discord.gg": "discord",
    ".heartbeat.chat": "heartbeat",
    ".meetup.com": "meetup",
    ".geneva.com": "geneva",
    ".discourse.group": "discourse",
}
_PLATFORM_EXACT = {
    "facebook.com": "facebook", "fb.com": "facebook", "m.facebook.com": "facebook",
    "slack.com": "slack", "join.slack.com": "slack",
    "discord.com": "discord", "discord.gg": "discord",
    "skool.com": "skool", "meetup.com": "meetup", "linkedin.com": "linkedin",
    "www.linkedin.com": "linkedin", "t.me": "telegram", "telegram.me": "telegram",
}
_CIRCLE_INFRA_LABELS = {
    "app", "login", "discover", "www", "help", "status", "signup", "sign-in",
    "auth", "api", "assets", "cdn", "marketing", "compass", "email", "mail",
    "circle-compass-assets", "assets-v2",
}


def platform_from_host(host: str) -> str | None:
    """Classify a host by name alone. Returns None when a fetch is needed."""
    host = (host or "").strip().lower().strip(".")
    if not host:
        return None
    if host == "circle.so" or host.endswith(".circle.so"):
        label = host[: -len(".circle.so")].split(".")[-1] if host != "circle.so" else ""
        return PLATFORM_CIRCLE_INFRA if label in _CIRCLE_INFRA_LABELS or not label else PLATFORM_CIRCLE
    if host in _PLATFORM_EXACT:
        return _PLATFORM_EXACT[host]
    for suffix, name in _PLATFORM_SUFFIXES.items():
        if host.endswith(suffix):
            return name
    return None  # a bare custom domain -- probe it to know


def _looks_like_circle_html(text: str) -> bool:
    t = (text or "").lower()
    return (
        "assets-v2.circle.so" in t
        or "circle-compass-assets.circle.so" in t
        or 'name="application-name" content="circle"' in t
    )


def detect_platform(url: str, *, session=None, timeout: int = 10) -> str:
    """Full platform detection: host heuristics, then a Circle-API probe.

    A Circle community -- on any domain -- answers ``/internal_api/spaces`` with
    JSON (200 when public, 401/403 when private). A non-Circle host answers with
    an HTML page or an error. That single request is the reliable test.
    """
    if not url or "://" not in url:
        if (url or "").startswith("discover.circle.so"):
            return PLATFORM_DISCOVER
        url = "https://" + (url or "")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return PLATFORM_OTHER  # manual://, jsonl:, ...
    host = (parsed.hostname or "").lower()
    if host == "discover.circle.so":
        return PLATFORM_DISCOVER
    named = platform_from_host(host)
    if named is not None:
        return named

    from circle_leads.scraper.http_client import shared_session

    http = session or shared_session()
    ua = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    }
    try:
        r = http.get(f"https://{host}/internal_api/spaces", headers=ua,
                     timeout=timeout, allow_redirects=True)
        if "json" in r.headers.get("content-type", "").lower():
            return PLATFORM_CIRCLE
    except Exception:  # noqa: BLE001 - network flake -> fall through to the HTML check
        pass
    try:
        r = http.get(f"https://{host}", headers=ua, timeout=timeout, allow_redirects=True)
        if _looks_like_circle_html(r.text):
            return PLATFORM_CIRCLE
    except Exception:  # noqa: BLE001
        pass
    return PLATFORM_OTHER


def is_circle_platform(platform: str | None) -> bool:
    """True when the harvest's public reader can read this community."""
    return platform == PLATFORM_CIRCLE


# A plausible public hostname: dot-separated DNS labels ending in an alphabetic
# TLD. Deliberately strict enough to throw out the things a URL field actually
# collects by accident -- IP literals, "localhost", prose that urlparse happily
# reports as a hostname -- and nothing more. Whether the host is *Circle* is not
# a question a regex can answer; detect_platform() answers it with a request.
_PLAUSIBLE_HOST_RX = re.compile(
    r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


def normalize_community_url(
    url: str, *, allow_custom_domains: bool = False
) -> tuple[str, str] | None:
    """Return (slug, canonical_url) for a community URL, else None.

    By default only ``*.circle.so`` hosts are accepted. That default exists to
    keep untrusted bulk sources (web search, scraped HTML) from filling the DB
    with rows that are not communities at all, and several callers depend on it.

    ``allow_custom_domains=True`` is the opt-in for sources the operator vouched
    for -- a hand-curated ``--from-file`` list, an explicit ``--url``. Circle
    communities on their own domain are the majority of the ones worth having
    (62% of directory-resolved communities; 48% of our ICP-fit rows and 31% of
    successful joins come from custom domains), and a ".circle.so" suffix test
    cannot see them. Under the opt-in this function only decides "is this a
    plausible community host at all"; whether it is really Circle is left to
    ``detect_platform()``, which settles it with a request instead of a guess.
    Hosts that ``platform_from_host`` can already name as some *other* platform
    (Slack, Facebook, Skool...) are still rejected here -- no request will make
    a Facebook group readable, and the name is decisive.

    Under the opt-in the returned slug is the host (``unique_slug_for_host``),
    because a slug is a key and a registrable label is not unique.
    """
    url = (url or "").strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
    except ValueError:
        return None
    # ".circle.so" keeps its exact old semantics either way: the subdomain is
    # the slug, infrastructure hosts are reserved, and a deeper host
    # (a.b.circle.so) is not a community.
    if host.endswith(".circle.so"):
        slug = host[: -len(".circle.so")]
        if not slug or slug in RESERVED_SLUGS or "." in slug:
            return None
        return slug, f"https://{host}"
    if not allow_custom_domains:
        return None
    if not _PLAUSIBLE_HOST_RX.match(host):
        return None
    named = platform_from_host(host)
    if named is not None and named != PLATFORM_CIRCLE:
        return None
    # The host itself, not host.split(".")[0] (commit f741b80: the first label
    # collapsed every www.* host onto one row) and not the registrable label
    # either (that collapsed acme.com/acme.io/acme.co.uk onto one row). See
    # unique_slug_for_host.
    return unique_slug_for_host(host), f"https://{host}"


def extract_from_text(
    text: str, *, source: str = "text", allow_custom_domains: bool = False
) -> list[DiscoveredCommunity]:
    """Pull Circle community URLs out of any public text or HTML.

    ``allow_custom_domains`` additionally picks up custom-domain communities,
    but only from links written with an explicit scheme. Free prose mentions
    every domain under the sun ("we use notion.so", a footer's privacy link),
    and a bare-hostname sweep over arbitrary HTML would enqueue all of them for
    a platform probe. An ``href``-style URL is at least a link someone wrote on
    purpose; ``communities_from_urls`` is the path for operator-supplied hosts.
    """
    # Keyed by host, not slug: two custom domains can share a slug-ish label
    # ("acme.com" and "acme.io") and are not the same community.
    seen: dict[str, DiscoveredCommunity] = {}
    for match in CIRCLE_HOST_RX.finditer(text or ""):
        normalized = normalize_community_url(match.group(0))
        if not normalized:
            continue
        slug, url = normalized
        seen.setdefault(dedupe_key(url, slug),
                        DiscoveredCommunity(slug=slug, url=url, source=source))
    if allow_custom_domains:
        for match in _EXPLICIT_URL_RX.finditer(text or ""):
            normalized = normalize_community_url(match.group(0), allow_custom_domains=True)
            if not normalized:
                continue
            slug, url = normalized
            seen.setdefault(dedupe_key(url, slug),
                            DiscoveredCommunity(slug=slug, url=url, source=source))
    return list(seen.values())


def communities_from_urls(
    urls, *, source: str = "cli", allow_custom_domains: bool = False
) -> list[DiscoveredCommunity]:
    """Turn operator-supplied URLs (one per item) into communities.

    Distinct from ``extract_from_text``: each item is a URL the operator typed,
    not prose to be mined, so a bare custom host ("community.acme.com") is taken
    at face value under the opt-in instead of needing an "https://" prefix.
    """
    seen: dict[str, DiscoveredCommunity] = {}
    for raw in urls or []:
        normalized = normalize_community_url(
            raw, allow_custom_domains=allow_custom_domains
        )
        if not normalized:
            logger.warning("Skipping URL that is not a community host: %s", str(raw)[:80])
            continue
        slug, url = normalized
        # Keyed by host (see extract_from_text): an operator list of custom
        # domains is exactly where same-label/different-host pairs show up.
        seen.setdefault(dedupe_key(url, slug),
                        DiscoveredCommunity(slug=slug, url=url, source=source))
    return list(seen.values())


def load_from_file(
    path: str | Path, *, allow_custom_domains: bool = False
) -> list[DiscoveredCommunity]:
    """Load a user-provided list of communities (one URL per line, or CSV).

    A user-supplied list is the most reliable discovery source: the user
    already knows these communities exist and intends to work with them. That
    is also why ``allow_custom_domains`` belongs here -- a hand-written list is
    vouched for, so a community on its own domain should not be dropped before
    ``detect_platform`` ever sees it.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Community list not found: {p}")

    out: list[DiscoveredCommunity] = []
    if p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                raw = row.get("url") or row.get("community_url") or ""
                normalized = normalize_community_url(
                    raw, allow_custom_domains=allow_custom_domains
                )
                if not normalized:
                    continue
                slug, url = normalized
                out.append(
                    DiscoveredCommunity(
                        slug=slug,
                        url=url,
                        name=row.get("name") or None,
                        description=row.get("description") or None,
                        price_label=row.get("price") or None,
                        source=f"file:{p.name}",
                    )
                )
        return out

    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        normalized = normalize_community_url(
            line, allow_custom_domains=allow_custom_domains
        )
        if not normalized:
            logger.warning("Skipping non-Circle URL: %s", line[:80])
            continue
        slug, url = normalized
        out.append(DiscoveredCommunity(slug=slug, url=url, source=f"file:{p.name}"))
    return out


def dedupe(communities: list[DiscoveredCommunity]) -> list[DiscoveredCommunity]:
    """Collapse repeats of the same community, keeping the first row's fields.

    Keyed on the host (``dedupe_key``), never on the slug. Slugs from custom
    domains used to be registrable labels, so `acme.com`, `acme.io` and
    `acme.co.uk` all shared one, and two thirds of such an import vanished here
    -- before the DB's unique-URL matching could see them.
    """
    seen: dict[str, DiscoveredCommunity] = {}
    for c in communities:
        key = dedupe_key(c.url, c.slug)
        if key not in seen:
            seen[key] = c
        else:
            existing = seen[key]
            existing.name = existing.name or c.name
            existing.description = existing.description or c.description
            existing.price_label = existing.price_label or c.price_label
    return list(seen.values())
