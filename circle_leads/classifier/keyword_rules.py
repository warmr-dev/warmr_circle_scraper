"""Layer 1: transparent rule-based hiring-intent detection.

The hard case is that hiring and job-seeking share almost all their
vocabulary. "I'm looking for a software engineer" and "I'm looking for a job
as a software engineer" differ by one prepositional phrase. Keyword presence
alone cannot separate them, so the rules below work on *roles*: who is doing
the searching, and who would perform the work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- Vocabulary -------------------------------------------------------------

ROLE_NOUNS = (
    r"(?:developer|engineer|programmer|coder|designer|devops|architect|"
    r"consultant|contractor|freelancer|agency|studio|team|cto|co-?founder|"
    r"cofounder|specialist|expert|dev)"
)

SEEK_VERB = r"(?:look(?:ing)?|search(?:ing)?|hunt(?:ing)?|seek(?:ing)?)"

# Phrases that name employment itself as the object of the search. These are
# the decisive job-seeker markers: the thing wanted is a job, not a person.
EMPLOYMENT_OBJECT = (
    r"(?:a\s+|an\s+|new\s+|full[- ]time\s+|part[- ]time\s+|freelance\s+|remote\s+)*"
    r"(?:job|jobs|work|role|roles|position|positions|opportunit(?:y|ies)|"
    r"employment|gig|gigs|opening|openings|vacancy|vacancies|project|projects|"
    r"client|clients|contract|contracts)"
)

FIRST_PERSON = r"(?:i|i'm|i am|im|me|my|myself)"
ORG_SUBJECT = r"(?:we|we're|we are|were|our|us|my company|my team|my startup|the team)"

# --- Hiring intent (positive) ----------------------------------------------

HIRING_PATTERNS: list[tuple[str, str, int]] = [
    # (name, regex, weight)
    (
        "org_seeking_role",
        rf"\b{ORG_SUBJECT}\b[^.!?]{{0,60}}?\b{SEEK_VERB}\s+for\b[^.!?]{{0,60}}?\b{ROLE_NOUNS}\b",
        40,
    ),
    ("looking_to_hire", r"\blook(?:ing)?\s+to\s+hire\b", 40),
    ("want_to_hire", r"\b(?:want|need|trying)\s+to\s+hire\b", 40),
    (
        "we_are_hiring",
        rf"\b{ORG_SUBJECT}\b[^.!?]{{0,40}}?\b(?:are\s+|is\s+)?hiring\b",
        40,
    ),
    ("hiring_a_role", rf"\bhiring\s+(?:(?:a|an|our|\d+)\s+)?(?:[\w-]+\s+){{0,4}}?{ROLE_NOUNS}\b", 38),
    (
        "need_a_role",
        rf"\b(?:need|needs|needed|require|requires)\s+(?:(?:a|an|\d+)\s+)?(?:[\w-]+\s+){{0,4}}?{ROLE_NOUNS}\b",
        35,
    ),
    ("role_wanted", rf"\b{ROLE_NOUNS}\s+(?:wanted|needed|required)\b", 38),
    (
        # Bare "looking for a <role>" with no employment object. Hiring intent:
        # the thing sought is a person, not a job. The seeker patterns below
        # subtract when the object turns out to be employment instead.
        "seeking_a_role_person",
        rf"\b{SEEK_VERB}\s+for\s+(?:(?:a|an|some|\d+)\s+)?(?:[\w,/-]+\s+){{0,5}}?{ROLE_NOUNS}\b",
        35,
    ),
    (
        "looking_for_someone_to",
        r"\blook(?:ing)?\s+for\s+(?:someone|somebody|a\s+team|a\s+person)\s+(?:who|to|that)\b",
        38,
    ),
    (
        "seeking_role_for_org",
        rf"\bseek(?:ing)?\s+(?:(?:a|an)\s+)?(?:[\w-]+\s+){{0,4}}?{ROLE_NOUNS}\b[^.!?]{{0,40}}?\b(?:for\s+(?:our|my|the)\b|to\s+join\b)",
        38,
    ),
    (
        "build_our_thing",
        r"\b(?:build|develop|create|design)\s+(?:our|my|the)\s+"
        r"(?:app|application|website|platform|product|mvp|api|backend|frontend|saas|portal|site|system)\b",
        30,
    ),
    ("recommend_a_role", rf"\b(?:recommend|referral|refer\s+me)\b[^.!?]{{0,50}}?{ROLE_NOUNS}\b", 25),
    ("anyone_available", rf"\bany(?:one|body)\b[^.!?]{{0,30}}?\bavailable\b", 22),
    ("open_role", r"\b(?:open\s+(?:role|position|req)|job\s+opening|we\s+have\s+an?\s+opening)\b", 35),
    ("join_our_team", r"\bjoin\s+(?:our|my|the)\s+(?:team|company|startup)\b", 30),
    ("budget_for_work", r"\b(?:budget|paying|pay|rate|compensation)\b[^.!?]{0,40}?\b(?:for\s+(?:this|the)\s+(?:work|project|build)|per\s+hour|/hr)\b", 20),
]

# --- Job seeking (negative) -------------------------------------------------

JOB_SEEKER_PATTERNS: list[tuple[str, str, int]] = [
    (
        "seeking_employment_object",
        rf"\b{SEEK_VERB}\s+for\s+{EMPLOYMENT_OBJECT}\b",
        -45,
    ),
    ("open_to_work", r"\bopen\s+to\s+(?:work|opportunities|new\s+roles?|offers)\b", -45),
    ("available_for_work", r"\bavailable\s+for\s+(?:work|hire|projects?|freelance|contract)\b", -40),
    ("seeking_employment", r"\bseeking\s+(?:employment|a\s+new\s+role|new\s+opportunit)", -45),
    ("need_a_job", r"\bneed\s+(?:a\s+)?(?:job|work|employment)\b", -45),
    (
        "i_am_a_role",
        rf"\b(?:i\s*am|i'm|im)\s+(?:a|an)\s+(?:[\w-]+\s+){{0,5}}?{ROLE_NOUNS}\b",
        -25,
    ),
    (
        "role_looking_for_work",
        rf"\b{ROLE_NOUNS}\b[^.!?]{{0,30}}?\b{SEEK_VERB}\s+for\s+{EMPLOYMENT_OBJECT}\b",
        -45,
    ),
    ("anyone_hiring", r"\b(?:any(?:one|body)|any\s+compan(?:y|ies))\s+hiring\b", -40),
    ("are_there_jobs", r"\b(?:any|are\s+there(?:\s+any)?)\s+(?:jobs?|openings?|opportunities|positions?)\s+(?:available|going|out\s+there)?\b", -40),
    ("dm_me_for_portfolio", r"\b(?:dm|message|contact)\s+me\b[^.!?]{0,40}?\b(?:portfolio|cv|resume|rates)\b", -35),
    ("years_of_experience_self", rf"\b(?:i\s+have|with)\s+\d+\+?\s+years?\s+(?:of\s+)?experience\b", -20),
    ("my_portfolio", r"\bmy\s+(?:portfolio|resume|cv|github)\b", -25),
    ("happy_to_help_promo", r"\b(?:i|we)\s+(?:can|could)\s+(?:help|build\s+(?:this|it)\s+for\s+you)\b", -30),
]

# --- Disqualifiers ----------------------------------------------------------

NEGATION_PATTERNS: list[tuple[str, str, int]] = [
    ("not_hiring", r"\b(?:not|aren'?t|isn'?t|no\s+longer|won'?t\s+be)\s+(?:currently\s+)?hiring\b", -40),
    ("hiring_freeze", r"\bhiring\s+(?:freeze|pause|paused|on\s+hold)\b", -40),
    ("filled_role", r"\b(?:role|position)\s+(?:has\s+been\s+)?filled\b", -40),
    ("closed_applications", r"\b(?:applications?|role)\s+(?:are\s+|is\s+)?closed\b", -35),
]

HYPOTHETICAL_PATTERNS: list[tuple[str, str, int]] = [
    ("hypothetical", r"\b(?:if\s+you\s+(?:were|are)\s+(?:hiring|looking)|hypothetically|imagine\s+(?:if|you))\b", -20),
    ("educational", r"\b(?:how\s+(?:do|would)\s+you\s+(?:go\s+about\s+)?(?:hire|hiring|find)|tips\s+for\s+hiring|guide\s+to\s+hiring|advice\s+on\s+hiring)\b", -20),
    ("quotation", r"^\s*(?:>|\")", -15),
]

DELIVERABLE_PATTERN = (
    r"\b(?:app|application|website|web\s*app|mobile\s*app|integration|api|"
    r"automation|platform|mvp|saas|dashboard|portal|backend|frontend|"
    r"landing\s*page|e-?commerce|store|system|software|prototype)\b"
)

BUDGET_PATTERN = (
    r"(?:\$\s?[\d,]+(?:\s?[-–—to]+\s?\$?[\d,]+)?(?:\s?[kK])?"
    r"|\b(?:usd|eur|gbp)\s?[\d,]+"
    r"|\b[\d,]+\s?(?:usd|eur|gbp)\b"
    r"|\bbudget\s+(?:of|is|around|approx\.?|~)?\s*\$?[\d,]+"
    r"|\b(?:\d+\s?[kK])\s+budget\b)"
)

TIMELINE_PATTERN = (
    r"\b(?:asap|urgent(?:ly)?|immediately|right\s+away|this\s+week|next\s+week|"
    r"by\s+(?:end\s+of\s+)?(?:the\s+)?(?:week|month|quarter|year|"
    r"january|february|march|april|may|june|july|august|september|october|"
    r"november|december)|deadline|before\s+(?:our|the)\s+launch|launch(?:ing)?\s+in)\b"
)

REFERRAL_PATTERN = (
    r"\b(?:recommend(?:ations?)?|referrals?|suggestions?|proposals?|estimates?|"
    r"quotes?|who\s+(?:should|can)\s+i\s+(?:talk|speak|reach)\s+to|"
    r"know\s+(?:anyone|someone|a\s+good))\b"
)

BUYER_CAPACITY_PATTERN = (
    r"\b(?:we|our\s+(?:company|team|startup|agency|business)|founder|ceo|cto|"
    r"i\s+run|i\s+own|my\s+(?:company|startup|business|agency|team)|"
    r"at\s+[A-Z][\w&.-]+)\b"
)

_HIRE_KEYWORD = r"\b(?:hiring|hire|looking\s+for|need|seeking|wanted|recruit)\b"


@dataclass
class RuleResult:
    """Outcome of the rule layer, before any LLM involvement."""

    score: int = 0
    hiring_matches: list[str] = field(default_factory=list)
    seeker_matches: list[str] = field(default_factory=list)
    disqualifiers: list[str] = field(default_factory=list)
    signals: dict[str, bool] = field(default_factory=dict)

    @property
    def has_hard_disqualifier(self) -> bool:
        return bool(self.disqualifiers)

    def verdict(self, escalation_threshold: int = 55) -> str:
        """Rule-only verdict. UNCERTAIN means 'ask the LLM'."""
        if self.has_hard_disqualifier:
            return "NOT_LEAD"
        if self.score >= escalation_threshold:
            return "LEAD"
        if self.score <= 0:
            return "NOT_LEAD"
        return "UNCERTAIN"


def _compile(patterns: list[tuple[str, str, int]]):
    return [(name, re.compile(rx, re.I | re.M), w) for name, rx, w in patterns]


_HIRING = _compile(HIRING_PATTERNS)
_SEEKER = _compile(JOB_SEEKER_PATTERNS)
_NEGATION = _compile(NEGATION_PATTERNS)
_HYPOTHETICAL = _compile(HYPOTHETICAL_PATTERNS)
_DELIVERABLE = re.compile(DELIVERABLE_PATTERN, re.I)
_BUDGET = re.compile(BUDGET_PATTERN, re.I)
_TIMELINE = re.compile(TIMELINE_PATTERN, re.I)
_REFERRAL = re.compile(REFERRAL_PATTERN, re.I)
_BUYER = re.compile(BUYER_CAPACITY_PATTERN)
_HIRE_KW = re.compile(_HIRE_KEYWORD, re.I)


_FIRST_PERSON_EMPLOYMENT = re.compile(
    rf"\b{FIRST_PERSON}\b[^.!?]{{0,40}}?\b{SEEK_VERB}\s+for\s+{EMPLOYMENT_OBJECT}\b",
    re.I,
)
_ROLE_SEEKING_EMPLOYMENT = re.compile(
    rf"\b{ROLE_NOUNS}\b[^.!?]{{0,30}}?\b{SEEK_VERB}\s+for\s+{EMPLOYMENT_OBJECT}\b",
    re.I,
)


def _first_person_seeking_employment(text: str) -> bool:
    """Detect the decisive job-seeker shape: a person wanting employment.

    This is what separates "looking for a software engineer" (hiring) from
    "looking for a job as a software engineer" (seeking) -- the object of the
    search is employment, not a person.
    """
    if _FIRST_PERSON_EMPLOYMENT.search(text):
        return True
    # "<role> looking for work" with no organizational subject
    return bool(_ROLE_SEEKING_EMPLOYMENT.search(text))


# Hiring intent, when present, appears early. Truncating bounds the work done
# on untrusted input without changing the verdict for real posts.
MAX_ANALYZED_CHARS = 20000


def analyze(text: str) -> RuleResult:
    """Score hiring intent from text alone. Deterministic and explainable."""
    result = RuleResult()
    if not text or not text.strip():
        return result
    if len(text) > MAX_ANALYZED_CHARS:
        text = text[:MAX_ANALYZED_CHARS]

    for name, rx, weight in _HIRING:
        if rx.search(text):
            result.score += weight
            result.hiring_matches.append(name)

    for name, rx, weight in _SEEKER:
        if rx.search(text):
            result.score += weight
            result.seeker_matches.append(name)

    for name, rx, weight in _NEGATION:
        if rx.search(text):
            result.score += weight
            result.disqualifiers.append(name)

    for name, rx, weight in _HYPOTHETICAL:
        if rx.search(text):
            result.score += weight
            result.disqualifiers.append(name)

    # The employment-object test is decisive on its own: it is the structure
    # that distinguishes a job seeker from a buyer using identical vocabulary.
    if _first_person_seeking_employment(text):
        result.score -= 45
        if "first_person_seeking_employment" not in result.seeker_matches:
            result.seeker_matches.append("first_person_seeking_employment")

    # "looking for X" fires both the hiring and the seeking pattern when X is
    # employment ("looking for work as a Flutter developer"). The employment
    # reading wins, so withdraw the hiring credit granted on the same span.
    if "seeking_a_role_person" in result.hiring_matches and (
        "seeking_employment_object" in result.seeker_matches
        or "first_person_seeking_employment" in result.seeker_matches
        or "role_looking_for_work" in result.seeker_matches
    ):
        result.score -= 35
        result.hiring_matches.remove("seeking_a_role_person")

    result.signals = {
        "deliverable": bool(_DELIVERABLE.search(text)),
        "budget": bool(_BUDGET.search(text)),
        "timeline": bool(_TIMELINE.search(text)),
        "referral_request": bool(_REFERRAL.search(text)),
        "buyer_capacity": bool(_BUYER.search(text)),
    }

    # Supporting signals only count when hiring language is actually present;
    # otherwise every product announcement would score as a lead.
    if result.hiring_matches:
        if result.signals["deliverable"]:
            result.score += 20
        if result.signals["budget"]:
            result.score += 15
        if result.signals["timeline"]:
            result.score += 10
        if result.signals["referral_request"]:
            result.score += 15
        if result.signals["buyer_capacity"]:
            result.score += 10

    return result


def matched_keywords(text: str, keywords: list[str]) -> list[str]:
    """Config-driven keyword matching, kept separate from the pattern rules."""
    low = (text or "").lower()
    return [k for k in keywords if k.lower() in low]


def has_hiring_vocabulary(text: str) -> bool:
    return bool(_HIRE_KW.search(text or ""))
