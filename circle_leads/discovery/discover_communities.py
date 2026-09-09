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


def community_slug_for_host(host: str) -> str:
    """A stable, collision-resistant community slug for any host.

    - `foo.circle.so`            -> `foo`   (the subdomain label)
    - `www.siliconslopes.com`    -> `siliconslopes`
    - `community.bigstarlights.com` -> `bigstarlights`
    - `siliconslopes.com`        -> `siliconslopes`

    The old `host.split(".")[0]` collapsed every `www.*` host to `www` and
    every `community.*` host to `community`, so unrelated communities landed
    on one row. Two-part public suffixes (`example.co.uk`) fall back to the
    label before the suffix pair, which is good enough here -- `get_or_create_
    community` also matches on the (unique) URL.
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
    # 3+ labels remain: assume the last two are a public suffix (co.uk, com.au).
    return labels[-3]


def normalize_community_url(url: str) -> tuple[str, str] | None:
    """Return (slug, canonical_url) for a Circle community URL, else None."""
    url = (url or "").strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if not host.endswith(".circle.so"):
        return None
    slug = host[: -len(".circle.so")]
    if not slug or slug in RESERVED_SLUGS or "." in slug:
        return None
    return slug, f"https://{host}"


def extract_from_text(text: str, *, source: str = "text") -> list[DiscoveredCommunity]:
    """Pull Circle community URLs out of any public text or HTML."""
    seen: dict[str, DiscoveredCommunity] = {}
    for match in CIRCLE_HOST_RX.finditer(text or ""):
        normalized = normalize_community_url(match.group(0))
        if not normalized:
            continue
        slug, url = normalized
        seen.setdefault(slug, DiscoveredCommunity(slug=slug, url=url, source=source))
    return list(seen.values())


def load_from_file(path: str | Path) -> list[DiscoveredCommunity]:
    """Load a user-provided list of communities (one URL per line, or CSV).

    A user-supplied list is the most reliable discovery source: the user
    already knows these communities exist and intends to work with them.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Community list not found: {p}")

    out: list[DiscoveredCommunity] = []
    if p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                raw = row.get("url") or row.get("community_url") or ""
                normalized = normalize_community_url(raw)
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
        normalized = normalize_community_url(line)
        if not normalized:
            logger.warning("Skipping non-Circle URL: %s", line[:80])
            continue
        slug, url = normalized
        out.append(DiscoveredCommunity(slug=slug, url=url, source=f"file:{p.name}"))
    return out


def dedupe(communities: list[DiscoveredCommunity]) -> list[DiscoveredCommunity]:
    seen: dict[str, DiscoveredCommunity] = {}
    for c in communities:
        if c.slug not in seen:
            seen[c.slug] = c
        else:
            existing = seen[c.slug]
            existing.name = existing.name or c.name
            existing.description = existing.description or c.description
            existing.price_label = existing.price_label or c.price_label
    return list(seen.values())
