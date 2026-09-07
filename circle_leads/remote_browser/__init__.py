"""Remote interactive browser: a persistent Chromium that lives on the server.

The session ORIGINATES here. You drive Circle's own login page inside this
browser through the dashboard, so no cookie or session token is ever exported
from another machine and replayed -- which is explicitly out of scope.
"""

from circle_leads.remote_browser.session import (
    ChallengeDetected,
    RemoteBrowser,
    RemoteBrowserUnavailable,
    SessionState,
)

__all__ = [
    "RemoteBrowser",
    "RemoteBrowserUnavailable",
    "ChallengeDetected",
    "SessionState",
]
