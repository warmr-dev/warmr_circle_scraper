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


def test_a_place_the_persona_does_not_state_is_never_taken_from_the_model():
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


def test_a_location_field_takes_the_personas_place_without_asking_the_model():
    """hrone and firefly, 2026-11-09: Circle's "Location" is a place search;
    the bot left it to a person although its persona says where it is."""
    llm = ScriptedLlm()
    res = resolve_form(_db(), "main", [_field("Location", "location")], llm=llm,
                       persona={**PERSONA, "location": "Vietnam"})
    assert res.answers == {"1": "Vietnam"} and res.complete
    assert not llm.calls




# --- what the facts do not settle (the user's decision, 2026-11-16) -----------


def test_what_the_facts_do_not_settle_is_made_up_and_marked_as_such():
    db = _db()
    llm = ScriptedLlm(CANNOT_ANSWER, "Remote")
    res = resolve_form(db, "4", [_field("Work Style", "select", choices=["Remote", "Hybrid"])],
                       llm=llm, persona=PERSONA)
    assert res.answers == {"1": "Remote"}
    assert "most plausibly" in llm.calls[1][0]          # the second call is the invent prompt
    with db.session() as s:
        ans = s.scalar(select(JoinFormAnswer))
        assert (ans.answer, ans.source, ans.reviewed) == ("Remote", "ai_invented", False)


def test_contact_details_and_links_are_never_made_up():
    for label, kind in [("Phone number", "text"), ("LinkedIn profile", "text"),
                        ("Website", "link"), ("Date of birth", "date")]:
        llm = ScriptedLlm(CANNOT_ANSWER, "made up")
        res = resolve_form(_db(), "4", [_field(label, kind)], llm=llm, persona=PERSONA)
        assert not res.complete, label
        assert len(llm.calls) <= 1, label


def test_a_single_box_is_never_ticked_on_a_made_up_basis():
    llm = ScriptedLlm(CANNOT_ANSWER, "true")
    res = resolve_form(_db(), "4", [_field("I am a licensed advisor", "checkbox")],
                       llm=llm, persona=PERSONA)
    assert not res.complete and len(llm.calls) == 1


def test_a_checkbox_field_with_choices_takes_several_of_them():
    options = ["New Graduate", "Manager", "Director", "VP", "CMO"]
    res = resolve_form(_db(), "4", [_field("Career Stage", "checkbox", choices=options)],
                       llm=ScriptedLlm("Manager\nDirector"), persona=PERSONA)
    assert res.answers == {"1": ["Manager", "Director"]}


def test_an_option_the_form_does_not_offer_is_dropped_from_a_multi_answer():
    options = ["Content Marketing", "SEO / Search"]
    res = resolve_form(_db(), "4", [_field("Superpowers", "checkbox", choices=options)],
                       llm=ScriptedLlm("Content Marketing\nAstrology", "NONE"), persona=PERSONA)
    assert res.answers == {"1": ["Content Marketing"]}


def test_an_opt_in_to_share_data_is_declined_without_asking_the_model():
    label = ("Stay Connected with Avaya, Opt-in below if you would like IAUG to share your "
             "contact information with Avaya")
    options = ["Yes, please share with my contact information with Avaya",
               "No, please do not share my contact information with Avaya"]
    llm = ScriptedLlm()
    res = resolve_form(_db(), "4", [_field(label, "select", choices=options)],
                       llm=llm, persona=PERSONA)
    assert res.answers == {"1": options[1]}
    assert llm.calls == []


def test_the_join_bots_persona_names_aslan_perdeshov():
    """The user's answer for account 4 (2026-11-16)."""
    persona = load_persona("4")
    assert (persona["first_name"], persona["last_name"]) == ("Aslan", "Perdeshov")
    assert persona["full_name"] == "Aslan Perdeshov"
    assert persona["company"] == "Rapid Developers"     # from default


def test_options_are_read_the_way_a_model_lists_them():
    options = ["Virtual education", "Networking", "Industry insights"]
    for reply in ("- Networking\n- Industry insights", "Networking, Industry insights",
                  "1. Networking\n2) Industry insights.", "Networking  \nIndustry insights"):
        res = resolve_form(_db(), "4", [_field("I joined for", "checkbox", choices=options)],
                           llm=ScriptedLlm(reply), persona=PERSONA)
        assert res.answers == {"1": ["Networking", "Industry insights"]}, reply
    one = resolve_form(_db(), "4", [_field("Member type", "select", choices=["Customer", "Partner"])],
                       llm=ScriptedLlm("Partner."), persona=PERSONA)
    assert one.answers == {"1": "Partner"}


def test_a_matched_question_whose_answer_fits_no_option_is_answered_on_its_own():
    """IAUG's "Please Select a Member Type" was matched to Marketing Talent
    Exchange's "I'm here as" and given its "Talent" (2026-11-16)."""
    db = _db()
    resolve_form(db, "4", [_field("I'm here as", "select", choices=["Talent", "Hiring", "Both"])],
                 llm=ScriptedLlm("Talent"), persona=PERSONA)
    # match -> #1; "Talent" maps to none of these; facts -> no; made up -> Partner
    llm = ScriptedLlm("1", "NONE", CANNOT_ANSWER, "Partner")
    res = resolve_form(db, "4", [_field("Please Select a Member Type", "select", fid="2",
                                        choices=["Customer", "Partner", "Avaya"])],
                       llm=llm, persona=PERSONA)
    assert res.answers == {"2": "Partner"}
    with db.session() as s:
        q = s.scalar(select(JoinFormQuestion).where(
            JoinFormQuestion.label == "Please Select a Member Type"))
        assert q.same_as_id is None
