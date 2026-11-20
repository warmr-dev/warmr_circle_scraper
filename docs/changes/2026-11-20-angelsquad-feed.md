# Read Angel Squad's own app

## Problem and change

Angel Squad left Circle. Members now sign in at https://app.angelsquad.co
with an emailed code. The feed is `GET https://api.angelsquad.co/api/community/feed`
and it accepts that site's session cookie. A Circle cookie is rejected.

The reader, the 2-minute check and the session save now treat
`app.angelsquad.co` (and the old `angel-squad.circle.so` host) as this app.
The extension, on an `angelsquad.co` tab, sends the cookies from
`app.angelsquad.co` and `api.angelsquad.co` instead of looking for
`_circle_session`. The first save moves the existing Angel Squad row onto
`app.angelsquad.co`. Post ids there are strings, so the watcher remembers
them in `recent_ids` and does not write them into the numeric watermark.

## Scope and operations

Code. The dashboard (session save) and the droplet worker and watcher (the
read) both have to be running this change before a saved Angel Squad session
is read. No migration. The operator signs in at the app and sends the
cookies; nothing here logs in or requests the email code.

Documentation added: `README.md`
Delivery record: `docs/changes/2026-11-20-angelsquad-feed.md`

## Verification and delivery

```sh
python -m pytest tests/test_angelsquad.py -q
```
