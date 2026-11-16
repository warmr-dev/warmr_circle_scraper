# Sign in on a login page that asks for the email option first

## Problem and change

On 2026-11-16 the droplet's join run tried community.marketingtalentexchange.com
(a free community, put in the queue by hand that day) and stopped:
`needs_login`, "clicking Log in neither signed in nor showed a form within
6.4s". The screenshot shows the community's login page: "Log in to your
account", one "Sign in with an email" button, "Sign up" and "Forgot your
password?" links, no field. The email box appears only after that button.

`_attempt_login` waited for an email or password field after "Log in" and gave
up before it ever looked at the pivot controls it already knew
(`LOGIN_PIVOT_TEXTS` includes "Sign in with an email" -- used only once a form
had been seen). While it waits for the form, the walk now clicks the email
option (`EMAIL_CHOICE_TEXTS`: the email entries of `LOGIN_PIVOT_TEXTS`) once.
"Sign up" is never clicked on that page.

## Scope and operations

Code only; the droplet's join timer runs it after a deploy. No migration.
The community rests a week after the handoff (`reach.HANDOFF_REST`); after the
deploy it is retried at once by hand with `--host`.

Documentation added: `docs/joining.md`
Delivery record: `docs/changes/2026-11-16-login-email-choice.md`

## Verification and delivery

```sh
python -m pytest tests/test_cdp_join.py tests/test_cdp_driver.py tests/test_cdp_join_browser.py -q
# 68 passed, 5 skipped. Without the change the new test stops with the
# production message: "clicking Log in neither signed in nor showed a form
# within 6.4s".
```
