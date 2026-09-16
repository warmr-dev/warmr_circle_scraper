"""Auto-join: enroll this operator's account in ICP-qualified free/paid Circle
communities so their posts become scrapeable.

Driven by ego-browser (Ego Lite), not a headless VPS bot: the operator's own
Mac Chromium is what actually clicks Join, reusing CIRCLE_EMAIL/
CIRCLE_PASSWORD (already the connector's account) only to log in on a host
that isn't already authenticated -- see ``circle_leads/join/ego_bridge.py``
and ``circle_leads/join/joiner.py``.
"""
