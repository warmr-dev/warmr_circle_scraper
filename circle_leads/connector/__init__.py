"""Local Circle Connector: runs on the user's computer, holds the authenticated
Circle browser session locally, and uploads only normalized content to Railway.

Nothing sensitive leaves this machine: no Circle password (the user logs in
themselves in a real browser), no cookies, no session tokens, no browser
profile. Only normalized post text + the non-sensitive connection state are
sent to the backend over authenticated HTTPS.
"""
