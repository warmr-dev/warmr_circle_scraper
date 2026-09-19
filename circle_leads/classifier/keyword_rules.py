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

# Job titles for back-office support staff. A community post hiring one of
# these is a staffing ad, not a buyer of software work: whoever fills the seat
# does the work in-house. They are listed separately from ROLE_NOUNS because
# the same words also name our *buyer* personas (requirements.yaml targets
# "Office Manager" and "Operations Manager"), so only the hiring shapes below
# -- the title as the object of a hire, or as a job-ad headline -- may act on
# them. An office manager asking for a developer must stay a lead.
ADMIN_SUPPORT_ROLES = (
    r"(?:office\s+(?:manager|administrator|coordinator|assistant)|"
    r"executive\s+(?:assistant|administrator)|personal\s+assistant|"
    r"admin(?:istrative)?\s+(?:assistant|coordinator|clerk|support)|"
    r"operations\s+(?:coordinator|assistant|administrator|clerk)|"
    r"receptionist|front\s+desk\s+[\w-]+|secretary|"
    r"data\s+entry\s+(?:clerk|operator|specialist)|"
    # "<domain> administrator" -- "Philanthropy Administrator", "Grants
    # Administrator". The domains are listed positively rather than as "any
    # word that is not on a technical blocklist": a blocklist has to name every
    # technology that can precede "administrator" (WordPress, Shopify, Jira,
    # Zapier, ...) and silently mislabels the ones it has not heard of. Missing
    # a back-office domain here only costs a suppression; wrongly claiming a
    # technical title costs a real lead.
    r"(?:office|facilities|building|practice|clinic|dental|medical|school|"
    r"church|grants?|philanthropy|fundraising|donor|payroll|benefits|"
    r"human\s+resources|hr|billing|invoicing|membership|enrolment|enrollment|"
    r"admissions|records|scheduling|contracts?|procurement|travel|"
    r"front\s+office|back\s+office|business)\s+administrator)"
)

# Positive evidence that the post wants software work done. This is what the
# admin-hire rule stands down for, so it is deliberately generous: a wrong
# stand-down only means a post is scored normally, while a missed one kills a
# buyer outright.
#
# Roles here are the unambiguously technical ones. The engagement nouns a
# software buyer also uses -- agency, studio, freelancer, designer -- are NOT
# listed, because a staffing agency advertising an office seat names itself
# with exactly those words; they are handled by SOFTWARE_VENDOR_SOUGHT below,
# which requires them to be the *object* of a hire rather than the poster.
TECHNICAL_ROLE_PATTERN = (
    r"\b(?:developer|engineer|programmer|coder|devops|sre|architect|"
    r"full[- ]?stack|front[- ]?end|back[- ]?end|software|web\s+dev\w*|"
    r"mobile\s+dev\w*|cto|technical\s+co-?founder|technical\s+lead|"
    r"tech\s+lead|data\s+scientist|data\s+engineer)\b"
    # Bare "dev" is a technical role in these communities, except in "business
    # dev" / "biz dev", which is sales.
    r"|(?<!business\s)(?<!biz\s)\bdevs?\b"
)

# The things we get paid to build. Narrower than DELIVERABLE_PATTERN on
# purpose: "system" and "store" also name the filing system and the retail
# store an office manager looks after, so they are not evidence of a software
# request on their own.
SOFTWARE_ARTIFACT = (
    r"(?:app|apps|application|website|web\s*app|mobile\s*app|api|platform|mvp|"
    r"saas|software|dashboard|portal|backend|frontend|landing\s*page|"
    r"e-?commerce\s+(?:site|store|shop)|integration|automation|prototype|"
    r"plugin|extension|chatbot|bot|site)"
)

# A request to have that artifact made or worked on: "build our booking app",
# "extend our plugin", "automate the onboarding flow".
SOFTWARE_BUILD_REQUEST = (
    r"\b(?:build|building|develop|developing|create|creating|code|coding|"
    r"design|designing|redesign|rebuild|rebuilding|revamp|migrate|migrating|"
    r"integrate|automate|maintain|extend|upgrade|fix|ship|launch)\s+"
    rf"(?:(?:a|an|the|our|my|this|new|custom)\s+){{0,2}}(?:[\w-]+\s+){{0,2}}?{SOFTWARE_ARTIFACT}\b"
)

# The vendor nouns, but only where the poster is looking for one. The qualifier
# lookahead is what separates "know a good web agency" from "our staffing
# agency is hiring an office manager": in the latter the agency is the employer,
# not the thing being bought.
SOFTWARE_VENDOR_SOUGHT = (
    r"\b(?:hire|hiring|need|needs|needed|looking\s+for|look\s+for|seeking|"
    r"seek|find|finding|know|knows|recommend|engage|bring\s+on)\s+"
    r"(?:(?:a|an|some|any|the|our|new|good|solid)\s+){0,2}"
    r"(?:(?!staffing|recruit\w*|talent|temp|temporary|placement|employment)"
    r"[\w-]+\s+){0,2}?"
    r"(?:agency|studio|freelancer|contractor|consultancy|dev\s+shop|"
    r"development\s+(?:shop|partner|team)|designer)\b"
)

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
        # "finding engineers", "need help finding a developer"
        "finding_a_role",
        rf"\b(?:find|finding|source|sourcing|recruit|recruiting)\s+"
        rf"(?:\w+[\s,]+){{0,4}}?{ROLE_NOUNS}s?\b",
        35,
    ),
    (
        # "bring on a developer", "onboard a dev", "take on a designer"
        "bring_on_a_role",
        rf"\b(?:bring\s+on(?:\s+board)?|onboard|take\s+on)\s+"
        rf"(?:\w+[\s,]+){{0,4}}?{ROLE_NOUNS}s?\b",
        35,
    ),
    (
        "seeking_someone_to",
        r"\b(?:look(?:ing)?\s+for|need|needs|needed|want(?:ing)?|seek(?:ing)?|hir(?:e|ing)|after)\s+"
        r"(?:someone|somebody|a\s+team|a\s+person|a\s+dev|help)\s+"
        # 'to explain'/'to clarify' is a question, not a commission.
        r"(?:who|that|with|to\s+(?!explain|clarify|confirm|tell|answer|remind|check\s+if)\w+)\b",
        38,
    ),
    (
        "seeking_role_for_org",
        rf"\bseek(?:ing)?\s+(?:(?:a|an)\s+)?(?:[\w-]+\s+){{0,4}}?{ROLE_NOUNS}\b[^.!?]{{0,40}}?\b(?:for\s+(?:our|my|the)\b|to\s+join\b)",
        38,
    ),
    (
        # "extend"/"upgrade" are here because work on an existing product is
        # commissioned as often as a new build ("extend our plugin"). The
        # bare-infinitive forms only: "we maintain our platform" is a vendor
        # describing itself, not a buyer.
        "build_our_thing",
        r"\b(?:build|develop|create|design|extend|upgrade)\s+(?:our|my|the)\s+"
        r"(?:app|application|website|platform|product|mvp|api|backend|frontend|"
        r"saas|portal|site|system|plugin|extension|chatbot)\b",
        30,
    ),
    ("recommend_a_role", rf"\b(?:recommend|referral|refer\s+me)\b[^.!?]{{0,50}}?{ROLE_NOUNS}\b", 25),
    (
        # "anyone know a good Flutter dev?" -- asking the room for a name is
        # how hiring usually starts in a community, and it names no vacancy.
        "anyone_know_a_role",
        rf"\b(?:any(?:one|body)|somebody|someone)\s+(?:here\s+)?(?:know|knows|worked\s+with|used|recommend)\b"
        rf"(?:\s+(?:a|an|any|some|of\s+a))?(?:\s+[a-z]+){{0,3}}?\s+{ROLE_NOUNS}\b",
        30,
    ),
    (
        # "we're rebuilding our app in Flutter" alongside a referral ask.
        "rebuilding_our_thing",
        r"\b(?:rebuild(?:ing)?|revamp(?:ing)?|redesign(?:ing)?|migrat(?:e|ing))\s+"
        r"(?:our|my|the)\s+"
        r"(?:app|application|website|platform|product|mvp|site|system|store)\b",
        25,
    ),
    ("anyone_available", rf"\bany(?:one|body)\b[^.!?]{{0,30}}?\bavailable\b", 22),
    ("open_role", r"\b(?:open\s+(?:role|position|req)|job\s+opening|we\s+have\s+an?\s+opening)\b", 35),
    # --- Job-post headline grammar --------------------------------------
    # Hiring in a projects/jobs space is often posted as a headline, not a
    # sentence: "Senior Data Engineer (Contract)", "2x DevOps Engineers",
    # "Snowflake freelancers wanted", "Power BI Developer — Contract". These
    # pair a role with an explicit hiring marker, so they don't fire on a
    # freelancer merely naming their own title.
    (
        # "<role> (Contract)" / "<role> - Contract Opportunity" / "<role>, Remote"
        "role_with_contract_marker",
        rf"{ROLE_NOUNS}\b[^.!?\n]{{0,40}}?\b(?:contract|contractor|freelance|"
        rf"part[- ]time|full[- ]time|w2|c2c|1099)\b",
        35,
    ),
    (
        # "<count>x <role>" or "<count> <role>s": "2x DevOps Engineers",
        # "3 Data Engineers" -- a count of people to bring on is a hire.
        "count_of_roles",
        rf"\b\d+\s?x?\s+(?:[\w/-]+\s+){{0,3}}?{ROLE_NOUNS}s?\b",
        35,
    ),
    (
        # "<skills> freelancers/contractors/experts wanted/needed"
        "skills_pros_wanted",
        rf"\b(?:{ROLE_NOUNS}s?|experts?|professionals?|pros?)\s+"
        rf"(?:wanted|needed|required|sought)\b",
        38,
    ),
    (
        # "Contract Opportunity", "Freelance Opportunity", "Remote Opportunity"
        # -- naming an opportunity offered (not sought) is a hiring post.
        "opportunity_offered",
        r"\b(?:contract|freelance|remote|consulting|project)\s+opportunit(?:y|ies)\b",
        30,
    ),
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
    (
        "available_for_work",
        r"\bavailable\s+for\s+(?:work|hire|projects?|freelance|contract|"
        # A seller offering consulting or client slots, same shape as the rest.
        r"consulting|new\s+clients?|clients?)\b",
        -40,
    ),
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
    (
        # "I'm looking for consulting opportunities" -- the same noun a hiring
        # post uses ("Contract Opportunity" below) with the roles reversed:
        # the poster is the one who wants to be engaged, so they are a seller.
        "seeking_opportunities_self",
        rf"\b{FIRST_PERSON}\b[^.!?]{{0,40}}?\b{SEEK_VERB}\s+for\s+"
        rf"(?:[\w-]+\s+){{0,3}}?opportunit(?:y|ies)\b",
        -45,
    ),
    (
        # A vendor announcing capacity. "Taking on new clients" has no reading
        # in which the poster is the one buying.
        "taking_on_clients",
        r"\b(?:taking|accepting|onboarding)\s+(?:on\s+)?(?:new\s+)?clients\b",
        -40,
    ),
]

# --- Disqualifiers ----------------------------------------------------------

NEGATION_PATTERNS: list[tuple[str, str, int]] = [
    ("not_hiring", r"\b(?:not|aren'?t|isn'?t|no\s+longer|won'?t\s+be)\s+(?:currently\s+)?hiring\b", -40),
    ("hiring_freeze", r"\bhiring\s+(?:freeze|pause|paused|on\s+hold)\b", -40),
    ("filled_role", r"\b(?:role|position)\s+(?:has\s+been\s+)?filled\b", -40),
    ("closed_applications", r"\b(?:applications?|role)\s+(?:are\s+|is\s+)?closed\b", -35),
]

# Recruitment-scam templates posted verbatim across many communities. They
# read as a hiring ask to a human and to the LLM alike (2026-09-19: the same
# "Collab Opportunity" text became a lead in surferseo twice and in engglobal),
# so they are disqualified before the LLM is asked. Keep each pattern to the
# template's own wording, so no genuine post can match it.
SCAM_PATTERNS: list[tuple[str, str, int]] = [
    ("scam_remote_partner", r"\breliable\s+partner\s*\(\s*must\s+be\s+based\s+in\b", -60),
]

HYPOTHETICAL_PATTERNS: list[tuple[str, str, int]] = [
    ("hypothetical", r"\b(?:if\s+you\s+(?:were|are)\s+(?:hiring|looking)|hypothetically|imagine\s+(?:if|you))\b", -20),
    ("educational", r"\b(?:how\s+(?:do|would)\s+you\s+(?:go\s+about\s+)?(?:hire|hiring|find)|tips\s+for\s+hiring|guide\s+to\s+hiring|advice\s+on\s+hiring)\b", -20),
    ("quotation", r"^\s*(?:>|\")", -15),
]

DELIVERABLE_PATTERN = (
    r"\b(?:app|application|website|web\s*app|mobile\s*app|integration|api|"
    r"automation|platform|mvp|saas|dashboard|portal|backend|frontend|"
    r"landing\s*page|e-?commerce|store|system|software|prototype|"
    r"plugin|extension|chatbot)\b"
)

BUDGET_PATTERN = (
    # A trailing k/K and a rate suffix are part of the figure: "$30k" and
    # "$50/hr" must not be reported as "$30" and "$50".
    r"(?:\$\s?[\d,]+(?:\.\d+)?(?:\s?[kKmM])?"
    r"(?:\s?(?:-|–|—|to)\s?\$?[\d,]+(?:\.\d+)?(?:\s?[kKmM])?)?"
    r"(?:\s?(?:/|per\s?)(?:hr|hour|day|week|month|year|yr))?"
    r"|\b(?:usd|eur|gbp)\s?[\d,]+(?:\s?[kKmM])?"
    r"|\b[\d,]+(?:\s?[kKmM])?\s?(?:usd|eur|gbp)\b"
    r"|\bbudget\s+(?:of|is|around|approx\.?|~)?\s*\$?[\d,]+(?:\s?[kKmM])?"
    r"|\b(?:\d+\s?[kKmM])\s+budget\b)"
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
    # "know a good/solid/reliable dev" -- the adjective varies, so accept
    # any short qualifier rather than hardcoding one.
    r"know\s+(?:anyone|someone|(?:of\s+)?an?(?:\s+[\w.+#-]+){0,3}?\s+(?:dev|developer|engineer|agency|freelancer|contractor|designer)))\b"
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
_SCAM = _compile(SCAM_PATTERNS)
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

# The admin title as the object of a hire: "we are hiring an Office Manager",
# "seeking an Administrative Assistant".
_ADMIN_HIRE_OBJECT = re.compile(
    rf"\b(?:hir(?:e|ing)|recruit(?:ing)?|{SEEK_VERB}\s+for|seek(?:ing)?|"
    rf"need(?:s|ed)?|want(?:ed|ing)?)\s+(?:(?:a|an|our|new|another|\d+)\s+)?"
    rf"(?:[\w-]+\s+){{0,2}}?{ADMIN_SUPPORT_ROLES}\b",
    re.I,
)
# The same hire written as a job-ad headline: "Office Manager - Full Time",
# "Administrative Assistant needed". The window is short and stops at sentence
# punctuation so it cannot reach across into an unrelated clause.
_ADMIN_HIRE_HEADLINE = re.compile(
    rf"{ADMIN_SUPPORT_ROLES}\b[^.!?\n]{{0,30}}?\b(?:wanted|needed|required|"
    rf"vacancy|vacancies|opening|position|full[- ]time|part[- ]time)\b",
    re.I,
)
# The vacancy named as a noun instead of a verb: "we have an opening for an
# Administrative Assistant", "new role for an Office Manager". Staffing agencies
# advertise in this voice, and the verb forms above never reach it.
_ADMIN_HIRE_VACANCY = re.compile(
    rf"\b(?:opening|openings|vacancy|vacancies|position|role|opportunity)\s+"
    rf"for\s+(?:(?:a|an|our|the|new|another)\s+)?(?:[\w-]+\s+){{0,2}}?"
    rf"{ADMIN_SUPPORT_ROLES}\b",
    re.I,
)
_TECHNICAL_ROLE = re.compile(TECHNICAL_ROLE_PATTERN, re.I)
_SOFTWARE_BUILD_REQUEST = re.compile(SOFTWARE_BUILD_REQUEST, re.I)
_SOFTWARE_VENDOR_SOUGHT = re.compile(SOFTWARE_VENDOR_SOUGHT, re.I)

ADMIN_SUPPORT_PENALTY = -45


def requests_software_work(text: str) -> bool:
    """Is there a request for software work anywhere in the post?

    Three independent shapes count: a technical role is named, something we
    build is asked for, or a vendor is being sought. Any one of them means the
    back-office rule must keep its hands off the post.
    """
    return bool(
        _TECHNICAL_ROLE.search(text)
        or _SOFTWARE_BUILD_REQUEST.search(text)
        or _SOFTWARE_VENDOR_SOUGHT.search(text)
    )


def _admin_hire_sentences(text: str) -> list[tuple[int, int]]:
    """Sentence spans that advertise a back-office support seat.

    Roughly 40% of the leads this pipeline has produced were job ads for office
    managers, executive assistants and the like. Whoever fills that seat does
    the work themselves, so the post is not a buyer of software development.

    The span returned is the whole sentence, not just the title, because the
    hiring credit such a post earns ("we are hiring", "join our team") is
    earned by that same sentence, and it is credit for the wrong kind of hire.
    Sentence-terminating punctuation is left outside the span so that masking
    it cannot join two sentences into one.
    """
    spans: list[tuple[int, int]] = []
    for rx in (_ADMIN_HIRE_OBJECT, _ADMIN_HIRE_HEADLINE, _ADMIN_HIRE_VACANCY):
        for m in rx.finditer(text):
            # rfind returns -1 when the match is in the first sentence, which
            # is the answer we want: the sentence starts at 0.
            start = max(text.rfind(c, 0, m.start()) for c in ".!?\n") + 1
            ends = [i for i in (text.find(c, m.end()) for c in ".!?\n") if i >= 0]
            spans.append((start, min(ends) if ends else len(text)))
    return spans


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    """Blank out spans, preserving offsets and surrounding punctuation."""
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars)


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

    for name, rx, weight in _SCAM:
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

    # Same collision on the other noun: "consulting opportunities" reads as an
    # offer ("opportunity_offered") until the poster turns out to be the one
    # looking for it, at which point the offer reading is wrong.
    if "opportunity_offered" in result.hiring_matches and (
        "seeking_opportunities_self" in result.seeker_matches
    ):
        result.score -= 30
        result.hiring_matches.remove("opportunity_offered")

    # A back-office vacancy is weighed, not fatal. It used to be a hard
    # disqualifier, which returned NOT_LEAD at 0.9 before scoring or LLM
    # escalation could run -- so "we're hiring an office manager, and we need
    # someone to build our booking app, budget $15k" died at rule score 83.
    # A post that asks for software work anywhere is left alone entirely; a
    # post that does not loses the hiring credit its admin-hire sentences
    # earned, and then takes the penalty. Mixed and borderline posts stay on
    # the normal path, where the score or the LLM decides.
    admin_spans = (
        [] if requests_software_work(text) else _admin_hire_sentences(text)
    )
    if admin_spans:
        masked = _mask(text, admin_spans)
        for name, rx, weight in _HIRING:
            if name in result.hiring_matches and not rx.search(masked):
                result.score -= weight
                result.hiring_matches.remove(name)
        result.score += ADMIN_SUPPORT_PENALTY

    result.signals = {
        "deliverable": bool(_DELIVERABLE.search(text)),
        "budget": bool(_BUDGET.search(text)),
        "timeline": bool(_TIMELINE.search(text)),
        "referral_request": bool(_REFERRAL.search(text)),
        "buyer_capacity": bool(_BUYER.search(text)),
        # Kept as a signal rather than a disqualifier so a reviewer can still
        # see why the score dropped without the post being killed outright.
        "admin_support_hire": bool(admin_spans),
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
