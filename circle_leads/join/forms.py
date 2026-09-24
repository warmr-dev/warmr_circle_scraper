"""Answers for the questions a community asks before it lets the bot in.

After a join, Circle opens "Create a profile" (``/settings/profile?new_state=
true``). Until it is saved every API call answers 400 "Please confirm before
proceeding", so the join is not usable. The form's fields come from
``/internal_api/signup/profile`` with a type, a required flag and, for choice
fields, the options. Most communities only require the name and time zone,
which Circle pre-fills. Some add their own required questions.

Those questions become a shared database (models.JoinFormQuestion /
JoinFormAnswer / JoinFormFill):

1. A question seen before reuses its stored answer for this account.
2. A new wording is first matched by the LLM against known questions ("Your
   role" is "Job title"), and reuses that answer.
3. Otherwise the LLM answers it, but only from the account's persona facts
   (config/join_personas.yaml). The answer is stored and reused next time.
4. When the facts don't cover it, the field is not filled and the community
   goes to a human; a person's answer can be stored the same way.

Only required fields are filled. Every fill is logged per community and account.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from circle_leads.storage.database import Database
from circle_leads.storage.models import (
    JoinFormAnswer,
    JoinFormFill,
    JoinFormQuestion,
    utcnow,
)

logger = logging.getLogger(__name__)

DEFAULT_PERSONAS_PATH = Path(__file__).resolve().parent.parent / "config" / "join_personas.yaml"
CANNOT_ANSWER = "CANNOT_ANSWER"
MAX_ANSWER_CHARS = 300

TEXT_TYPES = {"text", "textarea", "link", "url", "number", "email"}
CHOICE_TYPES = {"select", "radio", "dropdown"}
MULTI_CHOICE_TYPES = {"multi_select", "checkboxes", "checkbox_group"}
BOOL_TYPES = {"checkbox", "boolean", "toggle"}


@dataclass
class FormField:
    """One field of a community form, as Circle describes it."""

    id: str
    label: str
    field_type: str
    required: bool = False
    choices: list[str] = field(default_factory=list)
    description: str | None = None
    platform_field: bool = False

    @classmethod
    def from_payload(cls, raw: dict) -> "FormField":
        return cls(
            id=str(raw.get("id")),
            label=str(raw.get("label") or "").strip(),
            field_type=str(raw.get("field_type") or "text").strip().lower(),
            required=bool(raw.get("required")),
            choices=[str(c) for c in (raw.get("choices") or []) if str(c).strip()],
            description=(raw.get("description") or None),
            platform_field=bool(raw.get("platform_field")),
        )


@dataclass
class FormResolution:
    """What to type into the form, and what the bot may not answer."""

    answers: dict[str, Any] = field(default_factory=dict)  # field id -> value
    needs_human: list[FormField] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.needs_human


def normalise_label(label: str) -> str:
    text = re.sub(r"[*:?!.()\[\]\"'`]", " ", (label or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def question_key(f: FormField) -> str:
    kind = "choice" if f.field_type in CHOICE_TYPES | MULTI_CHOICE_TYPES else f.field_type
    return f"{kind}:{normalise_label(f.label)}"[:300]


def load_persona(account_key: str, path: str | Path | None = None) -> dict[str, Any]:
    """The facts this account may state: ``default`` merged with its own entry.

    Null values are dropped, so an unconfirmed fact is simply not a fact.
    """
    cfg = Path(path or os.environ.get("JOIN_PERSONAS_PATH") or DEFAULT_PERSONAS_PATH)
    if not cfg.exists():
        return {}
    data = yaml.safe_load(cfg.read_text()) or {}
    merged = dict(data.get("default") or {})
    merged.update((data.get("accounts") or {}).get(account_key) or {})
    return {k: v for k, v in merged.items() if v not in (None, "", [], {})}


# --- LLM ------------------------------------------------------------------------

_ANSWER_SYSTEM = f"""You fill in one required field of an online community's sign-up form on behalf of a member.
Use ONLY the facts you are given. Never invent a name, company, job, number, credential, location, link or experience.
If the facts do not answer the question truthfully, reply with exactly {CANNOT_ANSWER}.
Rules by field type:
- text/textarea: a short, plain, first-person answer under {MAX_ANSWER_CHARS} characters.
- link/url: a URL that appears in the facts, otherwise {CANNOT_ANSWER}.
- choice: exactly one of the listed options, copied verbatim, or {CANNOT_ANSWER}.
- checkbox: "true" only if it is agreeing to the community's rules, terms, guidelines or code of conduct, or the facts make the statement true; otherwise {CANNOT_ANSWER}.
Reply with the answer only, no quotes, no explanation."""

_MATCH_SYSTEM = """You compare form questions. Given a new question and a numbered list of known questions,
reply with the number of the known question that asks for the same information (so the same answer fits both),
or 0 if none does. Reply with the number only."""

_CHOICE_SYSTEM = """Pick the option that means the same as the given answer.
Reply with exactly one option copied verbatim, or NONE if no option fits."""


def make_form_llm():
    """The LLM used for form answers: OpenRouter when its key is set (the
    user's preferred route), else whatever make_backend() finds, else None."""
    from circle_leads.classifier.ai_classifier import OpenRouterBackend, make_backend

    if os.environ.get("OPENROUTER_API_KEY"):
        try:
            return OpenRouterBackend(max_tokens=200)
        except RuntimeError:
            pass
    return make_backend()


def _ask(llm, system: str, payload: dict) -> str:
    try:
        return (llm.complete(system, json.dumps(payload, ensure_ascii=False)) or "").strip()
    except Exception as exc:  # noqa: BLE001 - a flaky model call must not crash a join batch
        logger.warning("form LLM call failed: %s", exc.__class__.__name__)
        return ""


def _generate_answer(llm, persona: dict, f: FormField) -> str | None:
    if llm is None or not persona:
        return None
    kind = "choice" if f.field_type in CHOICE_TYPES else (
        "checkbox" if f.field_type in BOOL_TYPES else f.field_type
    )
    reply = _ask(llm, _ANSWER_SYSTEM, {
        "facts": persona,
        "question": f.label,
        "description": f.description,
        "field_type": kind,
        "options": f.choices or None,
    })
    reply = reply.strip().strip('"').strip()
    if not reply or reply.upper().startswith(CANNOT_ANSWER):
        return None
    return reply[:MAX_ANSWER_CHARS]


def _match_known(llm, f: FormField, known: list[JoinFormQuestion]) -> JoinFormQuestion | None:
    if llm is None or not known:
        return None
    listing = {str(i + 1): q.label for i, q in enumerate(known)}
    reply = _ask(llm, _MATCH_SYSTEM, {"new_question": f.label, "known_questions": listing})
    m = re.match(r"\s*(\d+)", reply)
    if not m:
        return None
    idx = int(m.group(1))
    return known[idx - 1] if 1 <= idx <= len(known) else None


def _fit_to_field(llm, f: FormField, answer: str) -> Any | None:
    """Turn a stored answer into a value this form accepts, or None."""
    if f.field_type in BOOL_TYPES:
        return True if answer.strip().lower() in {"true", "yes", "1"} else None
    if f.field_type in CHOICE_TYPES | MULTI_CHOICE_TYPES:
        if not f.choices:
            return None
        exact = {c.strip().lower(): c for c in f.choices}
        if answer.strip().lower() in exact:
            picked = exact[answer.strip().lower()]
        else:
            if llm is None:
                return None
            reply = _ask(llm, _CHOICE_SYSTEM, {"answer": answer, "options": f.choices}).strip().strip('"')
            picked = exact.get(reply.lower())
            if picked is None:
                return None
        return [picked] if f.field_type in MULTI_CHOICE_TYPES else picked
    if f.field_type in {"link", "url"} and not re.match(r"https?://", answer.strip()):
        return None
    if f.field_type in TEXT_TYPES:
        return answer.strip()[:MAX_ANSWER_CHARS]
    return None  # location, date, file upload, ... -- not something to guess


# --- the database ---------------------------------------------------------------


def _get_or_create_question(s, f: FormField, host: str | None) -> tuple[JoinFormQuestion, bool]:
    key = question_key(f)
    q = s.scalar(select(JoinFormQuestion).where(JoinFormQuestion.question_key == key))
    created = q is None
    if created:
        q = JoinFormQuestion(
            question_key=key, label=f.label, field_type=f.field_type,
            description=f.description, first_host=host, times_seen=0,
        )
        s.add(q)
        s.flush()
    q.times_seen = (q.times_seen or 0) + 1
    q.last_host = host
    q.example_choices = f.choices or q.example_choices
    q.updated_at = utcnow()
    return q, created


def _stored_answer(s, question_id: int, account: str) -> JoinFormAnswer | None:
    return s.scalar(
        select(JoinFormAnswer).where(
            JoinFormAnswer.question_id == question_id, JoinFormAnswer.account == account
        )
    )


def save_answer(db: Database, question_id: int, account: str, answer: str,
                *, source: str = "human") -> None:
    """Store (or correct) the answer an account gives to a question."""
    with db.session() as s:
        row = _stored_answer(s, question_id, account)
        if row is None:
            s.add(JoinFormAnswer(question_id=question_id, account=account,
                                 answer=answer, source=source,
                                 reviewed=source == "human"))
        else:
            row.answer = answer
            row.source = source
            row.reviewed = source == "human"
            row.updated_at = utcnow()


def resolve_form(
    db: Database,
    account: str,
    fields: list[FormField],
    *,
    host: str | None = None,
    community_id: int | None = None,
    llm=None,
    persona: dict | None = None,
) -> FormResolution:
    """Decide the value of every required field, or say which need a human."""
    persona = load_persona(account) if persona is None else persona
    result = FormResolution()
    with db.session() as s:
        for f in fields:
            if not f.required:
                continue
            q, created = _get_or_create_question(s, f, host)
            canonical_id = q.same_as_id or q.id
            ans = _stored_answer(s, canonical_id, account)

            if ans is None and created:
                known = [
                    k for k in s.scalars(
                        select(JoinFormQuestion).where(
                            JoinFormQuestion.id != q.id,
                            JoinFormQuestion.same_as_id.is_(None),
                        )
                    ).all()
                    if (k.field_type in CHOICE_TYPES | MULTI_CHOICE_TYPES)
                    == (f.field_type in CHOICE_TYPES | MULTI_CHOICE_TYPES)
                ]
                twin = _match_known(llm, f, known)
                if twin is not None:
                    q.same_as_id = twin.id
                    canonical_id = twin.id
                    ans = _stored_answer(s, canonical_id, account)

            if ans is None:
                generated = _generate_answer(llm, persona, f)
                if generated is not None:
                    ans = JoinFormAnswer(question_id=canonical_id, account=account,
                                         answer=generated, source="ai", reviewed=False)
                    s.add(ans)
                    s.flush()

            value = _fit_to_field(llm, f, ans.answer) if ans is not None else None
            if value is None:
                result.needs_human.append(f)
            else:
                result.answers[f.id] = value
            s.add(JoinFormFill(
                community_id=community_id, host=host, account=account,
                question_id=q.id, label=f.label,
                answer=None if value is None else json.dumps(value, ensure_ascii=False),
                outcome="needs_human" if value is None else "answered",
            ))
    return result
