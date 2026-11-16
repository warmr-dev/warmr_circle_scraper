# Profile forms: made-up answers, Circle's choice widgets, account 4's persona

## Problem and change

On 2026-11-16 the join bot stopped twice on the profile step:
community.iaug.org (11 required questions unanswered) and
community.marketingtalentexchange.com (6). The model was allowed to answer only
from the persona's facts; account 4 had no persona of its own; and two kinds
of question could not be answered or filled at all.

The user's decisions that day: tick boxes; when the facts do not settle a
required question, let the model make up a plausible answer; account 4 is
Aslan Perdeshov; fill in every missing fact.

- **Made-up answers** (`forms.py`). Facts first; when they do not settle a
  required question, a second prompt asks for the most plausible answer for
  the persona, stored with source `ai_invented` (reviewable in
  `join_form_answers`). Never for a contact detail, link, ID, date or a single
  yes/no box (`may_invent`). Asked for people or companies the member knows,
  the model names none. Only required fields, as before.
- **Opt-ins declined** (`decline_option`): a question about sharing the
  member's data or receiving marketing takes its declining option, source
  `rule`, without the model.
- **Checkbox fields with choices** are a group, not one yes/no box: the model
  picks one to three options, stored one per line; a listed reply ("- A",
  "A, B", "1. A.") is read as options.
- **A matched question whose answer fits none of this form's options** is
  answered on its own: IAUG's "Please Select a Member Type" had been matched to
  Marketing Talent Exchange's "I'm here as" and given "Talent".
- **Circle's choice widgets** (`cdp_join.py`), measured on the live profile
  page (read-only, the bot's browser on the droplet): a one-option field is a
  `button[role=combobox]` over `[role=option]`; a checkbox field with choices is
  a group of `span[role=checkbox]`, each named by its `<label>`. The walk typed
  the answer into the combobox's hidden input and could not tick a box. It now
  opens the list and clicks the option, and ticks each box; an option the form
  does not offer is never typed in.
- **Account 4's persona** (`config/join_personas.yaml`): Aslan Perdeshov,
  Business Development Manager at Rapid Developers, with level, company size,
  industry, city, work style, skills and the rest filled in to be plausible.

## Scope and operations

Code and configuration; the droplet's join run uses them after a deploy. No
migration (`join_form_answers.source` is 16 characters; `ai_invented` is 11).
Stored answers keep winning over new ones: account 4's stored "First Name" =
"Erkebulan" is corrected in production by hand (see `WORKLOG.md`).

Preview with the real model on a throwaway SQLite database, the questions read
from production: every required question of Marketing Talent Exchange gets an
answer; IAUG leaves "State/Province" (US and Canada only) and its non-Avaya
products question to a person.

Documentation added: `docs/joining.md`
Delivery record: `docs/changes/2026-11-16-form-answers-invent.md`

## Verification and delivery

```sh
python -m pytest tests/test_join_forms.py tests/test_cdp_join.py \
  tests/test_cdp_driver.py tests/test_cdp_join_browser.py -q
# all pass. With forms.py and cdp_join.py reverted, the new tests fail.
python -m pytest -q
# all pass except test_launchd_installer.py (1 failed + 4 errors), the same
# as on clean main in this environment.
```
