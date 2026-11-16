"""A Discover product page, as discover.circle.so serves it (2026-11-16).

Only the parts the readers use: the schema.org Product offers, the listing's
Join button (an anchor around ``data-testid="listing-cta..."``), the
``external_url`` in the Next.js page data, and that data's quoted references
("$33") that a dollar-sign regex took for a price.
"""

import json


def card_html(*, link=None, offers=(), external=None, nextjs_refs=True):
    graph = [{
        "@type": "Product",
        "name": "A listing",
        "offers": [
            {"@type": "Offer",
             "priceSpecification": {"price": f"{price:.2f}", "priceCurrency": "USD",
                                    **({"unitCode": unit} if unit else {})}}
            for price, unit in offers
        ],
    }]
    parts = [
        "<html><head><title>A listing | Circle Discover</title>",
        '<script type="application/ld+json">'
        + json.dumps({"@context": "https://schema.org", "@graph": graph})
        + "</script></head><body>",
    ]
    if link:
        parts.append(
            f'<div role="separator"></div><a target="_blank" rel="noopener noreferrer" '
            f'class="w-full" href="{link.replace("&", "&amp;")}"><button type="button" '
            f'tabindex="0" data-testid="listing-cta-button">Join</button></a>')
    if nextjs_refs:
        parts.append(r'<script>self.__next_f.push([1,"{\"d\":\"$33\",\"fill\":\"$26\"}"])</script>')
    if external:
        parts.append(r'<script>self.__next_f.push([1,"\"external_url\":\"'
                     + external.replace("&", r"&") + r'\""])</script>')
    parts.append("</body></html>")
    return "".join(parts)
