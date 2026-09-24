"""The shared database of community form answers (circle_leads/join/forms.py).

The LLM is a scripted fake: each test says exactly what the model replies, so
what is asserted is the bookkeeping around it -- reuse, storage, the audit log,
and that nothing outside the persona's facts or the form's options gets typed.
"""

from __future__ import annotations

import json
import tempfile

from sqlalchemy import select

from circle_leads.join.forms import (
    CANNOT_ANSWER, FormField, load_persona, normalise_label, question_key,
    resolve_form, save_answer,
)
from circle_leads.storage.database import Database
from circle_leads.storage.models import JoinFormAnswer, JoinFormFill, JoinFormQuestion

PERSONA = {"company": "Acme Dev", "role": "Business development",
           "company_website": "https://acme.example"}


class ScriptedLlm:
    """Replies in order; records every (system, payload) it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, json.loads(user)))
        return self.replies.pop(0) if self.replies else ""


def _db():
    return Database("sqlite:///" + tempfile.mktemp(suffix=".db"))


def _field(label, field_type="text", *, fid="1", required=True, choices=None):
    return FormField(id=fid, label=label, field_type=field_type, required=required,
                     choices=choices or [])


def test_labels_normalise_so_punctuation_and_case_do_not_split_a_question():
    assert normalise_label("Company Name *") == normalise_label("company name:")
    assert question_key(_field("Your role?")) == "text:your role"
    # Single and multi choice share a key: the answer is picked per form anyway.
    assert question_key(_field("Industry", "select")) == question_key(_field("Industry", "multi_select"))


def test_persona_merges_default_and_account_and_drops_unconfirmed_facts(tmp_path):
    cfg = tmp_path / "p.yaml"
    cfg.write_text(
        "default:\n  company: null\n  reason_for_joining: Learn\n"
        "accounts:\n  main:\n    role: BD\n"
    )
    assert load_persona("main", cfg) == {"reason_for_joining": "Learn", "role": "BD"}
    assert load_persona("test", cfg) == {"reason_for_joining": "Learn"}


def test_optional_fields_are_left_alone():
    db = _db()
    llm = ScriptedLlm()
    res = resolve_form(db, "main", [_field("Headline", required=False)], llm=llm, persona=PERSONA)
    assert res.answers == {} and res.complete
    assert llm.calls == []


def test_a_new_question_is_answered_from_the_persona_stored_and_reused():
    db = _db()
    llm = ScriptedLlm("Acme Dev")
    first = resolve_form(db, "main", [_field("Company")], host="a.circle.so", llm=llm, persona=PERSONA)
    assert first.answers == {"1": "Acme Dev"}
    assert len(llm.calls) == 1  # no known questions yet, so no matching call

    with db.session() as s:
        ans = s.scalar(select(JoinFormAnswer))
        assert (ans.answer, ans.source, ans.reviewed, ans.account) == ("Acme Dev", "ai", False, "main")

    # Same question on another community: the stored answer, no model call.
    again = resolve_form(db, "main", [_field("company:", fid="9")], host="b.circle.so",
                         llm=ScriptedLlm(), persona=PERSONA)
    assert again.answers == {"9": "Acme Dev"}
    with db.session() as s:
        q = s.scalar(select(JoinFormQuestion))
        assert q.times_seen == 2 and q.first_host == "a.circle.so" and q.last_host == "b.circle.so"


def test_answers_are_per_account():
    db = _db()
    resolve_form(db, "main", [_field("Company")], llm=ScriptedLlm("Acme Dev"), persona=PERSONA)
    llm = ScriptedLlm("Acme Dev")
    res = resolve_form(db, "test", [_field("Company")], llm=llm, persona=PERSONA)
    # "test" had no answer of its own, so the model was asked for one.
    assert res.answers == {"1": "Acme Dev"}
    assert len(llm.calls) == 1 and "facts" in llm.calls[0][1]
    with db.session() as s:
        assert sorted(a.account for a in s.scalars(select(JoinFormAnswer))) == ["main", "test"]


def test_what_the_facts_do_not_cover_goes_to_a_human_and_is_logged():
    db = _db()
    res = resolve_form(db, "main", [_field("Annual revenue")], host="a.circle.so",
                       community_id=None, llm=ScriptedLlm(CANNOT_ANSWER), persona=PERSONA)
    assert not res.complete
    assert [f.label for f in res.needs_human] == ["Annual revenue"]
    with db.session() as s:
        assert s.scalar(select(JoinFormAnswer)) is None
        fill = s.scalar(select(JoinFormFill))
        assert (fill.outcome, fill.answer, fill.host) == ("needs_human", None, "a.circle.so")


def test_without_persona_facts_the_model_is_not_even_asked():
    db = _db()
    llm = ScriptedLlm("made up")
    res = resolve_form(db, "main", [_field("Company")], llm=llm, persona={})
    assert not res.complete
    assert llm.calls == []


def test_a_human_answer_is_used_as_is():
    db = _db()
    first = resolve_form(db, "main", [_field("Referral code")], llm=ScriptedLlm(CANNOT_ANSWER),
                         persona=PERSONA)
    assert not first.complete
    with db.session() as s:
        qid = s.scalar(select(JoinFormQuestion.id))
    save_answer(db, qid, "main", "FRIEND2026")
    again = resolve_form(db, "main", [_field("Referral code")], llm=ScriptedLlm(), persona=PERSONA)
    assert again.answers == {"1": "FRIEND2026"}
    with db.session() as s:
        ans = s.scalar(select(JoinFormAnswer))
        assert (ans.source, ans.reviewed) == ("human", True)


def test_a_paraphrase_reuses_the_known_answer():
    db = _db()
    resolve_form(db, "main", [_field("Job title")], llm=ScriptedLlm("Business development"),
                 persona=PERSONA)
    llm = ScriptedLlm("1")  # "Your role" asks the same as known question #1
    res = resolve_form(db, "main", [_field("Your role", fid="7")], llm=llm, persona=PERSONA)
    assert res.answers == {"7": "Business development"}
    assert len(llm.calls) == 1
    with db.session() as s:
        role = s.scalar(select(JoinFormQuestion).where(JoinFormQuestion.label == "Your role"))
        title = s.scalar(select(JoinFormQuestion).where(JoinFormQuestion.label == "Job title"))
        assert role.same_as_id == title.id


def test_a_choice_is_always_one_of_this_forms_options():
    db = _db()
    options = ["Agency", "SaaS", "Other"]
    res = resolve_form(db, "main", [_field("Business type", "select", choices=options)],
                       llm=ScriptedLlm("Agency"), persona=PERSONA)
    assert res.answers == {"1": "Agency"}

    # Another community words its options differently: the stored "Agency" is
    # mapped onto one of its own options, never typed as-is.
    other = ["Service business / agency", "Software company"]
    llm = ScriptedLlm("Service business / agency")
    res2 = resolve_form(db, "main", [_field("Business type", "select", fid="2", choices=other)],
                        llm=llm, persona=PERSONA)
    assert res2.answers == {"2": "Service business / agency"}


def test_a_choice_the_model_makes_up_is_rejected():
    db = _db()
    res = resolve_form(db, "main", [_field("Industry", "select", choices=["Health", "Retail"])],
                       llm=ScriptedLlm("Software", "Software"), persona=PERSONA)
    assert not res.complete


def test_a_link_must_be_a_url():
    db = _db()
    res = resolve_form(db, "main", [_field("Website", "link")], llm=ScriptedLlm("acme.example"),
                       persona=PERSONA)
    assert not res.complete
    ok = resolve_form(_db(), "main", [_field("Website", "link")],
                      llm=ScriptedLlm("https://acme.example"), persona=PERSONA)
    assert ok.answers == {"1": "https://acme.example"}


def test_a_checkbox_is_only_ever_ticked():
    db = _db()
    res = resolve_form(db, "main", [_field("I agree to the community guidelines", "checkbox")],
                       llm=ScriptedLlm("true"), persona=PERSONA)
    assert res.answers == {"1": True}
    refused = resolve_form(_db(), "main", [_field("I am a licensed advisor", "checkbox")],
                           llm=ScriptedLlm(CANNOT_ANSWER), persona=PERSONA)
    assert not refused.complete


def test_a_field_type_the_bot_does_not_fill_goes_to_a_human():
    db = _db()
    res = resolve_form(db, "main", [_field("Where are you based", "location")],
                       llm=ScriptedLlm("Jakarta"), persona=PERSONA)
    assert not res.complete


def test_a_failing_model_call_is_a_handoff_not_a_crash():
    class Broken:
        def complete(self, system, user):
            raise TimeoutError("upstream")

    res = resolve_form(_db(), "main", [_field("Company")], llm=Broken(), persona=PERSONA)
    assert not res.complete
