// Runs inside ego-browser's embedded Node runtime (`ego-browser nodejs`), one
// join attempt per process -- see circle_leads/join/ego_bridge.py, which pipes
// this file in via stdin after substituting the __EGO_JOIN_*__ tokens below
// for JSON-encoded literals.
//
// Not read from process.env: ego-browser's embedded runtime does not
// propagate custom environment variables into the script (confirmed live --
// only a safelisted subset such as PATH/HOME comes through), so parameters
// (including CIRCLE_EMAIL/CIRCLE_PASSWORD, when configured) must be injected
// as literal source text instead. Never console.log the password -- every
// emitted message below is credential-free by construction.
//
// Circle sessions from *.circle.so subdomains do NOT carry over to a custom
// domain (confirmed live: logging in on community.practicommunity.com did
// not authenticate community.surferseo.com) -- so a login prompt on a new
// host is expected, not a bug, and is handled here rather than always
// treated as a stop condition. What still stops the batch and hands the
// browser back to the operator: no CIRCLE_EMAIL/CIRCLE_PASSWORD configured,
// an unrecognized login form, a Cloudflare/human-verification challenge (no
// bypass attempted, ever), or a custom application form this version has no
// persona-answer wiring for -- matching the stop-don't-evade posture already
// used in circle_leads/remote_browser/.
//
// Emits exactly one line, "EGO_JOIN_RESULT=<json>", as the last line of
// stdout: { status, detail, screenshot, cookies }. `status` is one of:
//   joined, paid_skip, pending_approval,
//   subscription_expired_skip, invite_skip            -- terminal, DB-recorded
//   needs_login, challenge_stop,
//   application_form_detected, unclear               -- handoff, not recorded
// `cookies` (only ever non-null on "joined") is this host's session cookies --
// see captureSessionCookies() -- so the caller can wire the new membership
// straight into circle_leads/web/replay_store.py and the existing
// scan_cookie_host()/cookie_hosts_vip_first() scan path, same as a manually
// pasted cookie. Never logged anywhere except this one JSON line.

const spaceId = __EGO_JOIN_SPACE_ID__;
const url = __EGO_JOIN_URL__;
const screenshotDir = __EGO_JOIN_SCREENSHOT_DIR__ || "";
const loginEmail = __EGO_JOIN_EMAIL__;
const loginPassword = __EGO_JOIN_PASSWORD__;

const JOIN_TEXTS = [
  "Join", "Sign up", "Get access", "Join for free", "Request to join",
  "Accept invitation", "Register for Free", "Register", "Join space",
  // A discover.circle.so product-listing page (confirmed live: saasrise) has
  // no "Join" control at all -- for a paid community it's just "Buy", which
  // goes straight to /checkout and is caught by the existing onCheckout
  // guard below (paid_skip), same as any other paid host.
  "Buy",
];
const EMAIL_SELECTOR =
  "input[type=email], input[name*=email i], input[autocomplete=email], input[autocomplete=username]";
const PASSWORD_SELECTOR = "input[type=password]";
// Mirrors circle_leads/scraper/member_api_reader.py::SESSION_COOKIE_NAMES --
// the only cookies that carry a member session (cf_clearance etc deliberately
// excluded there too: not required by /internal_api, and IP/device-bound).
const SESSION_COOKIE_NAMES = ["_circle_session", "remember_user_token", "user_session_identifier"];

function emit(status, detail, screenshot, cookies) {
  console.log(
    "EGO_JOIN_RESULT=" +
      JSON.stringify({ status, detail, screenshot: screenshot || null, cookies: cookies || null })
  );
}

/** Only ever called right before emitting "joined" -- confirmed live
 * (page.cdp("Network.getCookies", {}) with no `urls` scopes to the current
 * page) that this returns exactly the current host's cookies, no Network.enable
 * needed first. Never throws: a capture failure shouldn't turn a real join
 * into a worse outcome than "joined, but replay_store wasn't updated". */
async function captureSessionCookies(page) {
  try {
    const result = await page.cdp("Network.getCookies", {});
    return (result.cookies || [])
      .filter((c) => SESSION_COOKIE_NAMES.includes(c.name))
      .map((c) => ({ name: c.name, value: c.value, domain: c.domain }));
  } catch {
    return [];
  }
}

async function maybeScreenshot(page, tag) {
  if (!screenshotDir) return null;
  const path = `${screenshotDir}/${tag}-${Date.now()}.png`;
  await page.screenshot({ path });
  return path;
}

/** A page mid-navigation (a redirect chain right after goto/login/submit,
 * confirmed live on otto-mates' and sellyoursmarts' invitation-link
 * redirects -- the latter needed longer than one retry) can briefly have no
 * `document`/`document.body` -- evaluate() then throws instead of returning
 * a value. Retry a few times with a growing wait rather than crashing the
 * whole attempt on what is just a slow redirect chain settling. */
async function evaluateWithRetry(page, fn) {
  const isTransient = (err) => /reading 'innerText'|reading 'body'|Cannot read properties of null/.test(String(err));
  for (const delayMs of [800, 1600, 2400]) {
    try {
      return await page.evaluate(fn);
    } catch (err) {
      if (!isTransient(err)) throw err;
      await page.waitForTimeout(delayMs);
    }
  }
  return page.evaluate(fn); // last attempt -- let a real error surface now
}

async function readPageState(page) {
  return evaluateWithRetry(page, () => {
    const clickable = [...document.querySelectorAll("button, a")]
      .map((el) => (el.textContent || "").trim())
      .filter(Boolean);
    const has = (patterns) => clickable.some((t) => patterns.some((p) => p.test(t)));
    const bodyText = document.body.innerText || "";
    return {
      // Confirmed live (agency-mavericks): a private/invite-only community
      // shows "This is a private community -- it doesn't look like you have
      // access ... the admin may need to add you as a member". We're already
      // authenticated platform-wide (the page even names our email) but this
      // specific community has no self-serve join at all. This page also has
      // an inline "sign in with a different email" link, whose text alone
      // (just "sign in") used to false-positive hasLogin below and send this
      // into attemptLogin -- which then correctly failed to click it (it's
      // prose, not a real login control), but as a wrong reason.
      inviteOnlyGate:
        /this is a private community/i.test(bodyText) &&
        /doesn.?t look like you have access|admin may need to add you/i.test(bodyText),
      hasLogin: has([/^log in$/i, /^sign in$/i]),
      hasJoin: has([
        /^join$/i, /^join for free$/i, /^sign up$/i, /^get access$/i,
        /^request to join$/i, /^accept invitation$/i, /^register for free$/i, /^register$/i,
        /^buy$/i,
        // Confirmed live (the-technical-freelancer-academy): a community with
        // public space previews shows a per-space "Join space" CTA instead of
        // any page-level Join/Sign up control.
        /^join space$/i,
      ]),
      // Must be a <button>, not just anything with that aria-label -- a
      // plain [aria-label="Notifications"] false-positived live on
      // engglobal, a Circle-unrelated marketing page: a generic toast/alert
      // viewport div (role="region" aria-live="polite" aria-label=
      // "Notifications", a common Radix/shadcn-style pattern) matched it.
      // The Like/Comment/Bookmark fallback is needed too -- confirmed live
      // (awithub): a custom-themed community's header can skip those three
      // icons entirely, but per-post interaction controls (member-only)
      // still give an unambiguous real-membership signal.
      hasMemberNav:
        !!document.querySelector('button[aria-label="Notifications"]') ||
        !!document.querySelector('button[aria-label="Direct messages"]') ||
        !!document.querySelector('button[aria-label="User menu options"]') ||
        [...document.querySelectorAll("[aria-label]")].some((el) =>
          /^(like the|comment on|bookmark)/i.test(el.getAttribute("aria-label") || "")
        ),
      onCheckout: /\/checkout(\/|$)/.test(location.pathname),
      // Confirmed live (founderscupid): Circle itself redirects here when
      // the *community operator's* own Circle subscription has lapsed --
      // nothing to log into or click, the community is gone until the
      // operator pays again. Distinct from paid_skip (that's about OUR
      // account not paying for a still-live community).
      subscriptionExpired: /\/subscription_expired(\/|$)/.test(location.pathname),
      // Confirmed live (sellyoursmarts): re-navigating straight to the
      // candidate URL can itself land here if a previous attempt reached
      // "Accept invitation" but never finished the profile-setup step --
      // same signal as in readPostClickState, needed here too so the
      // initial-state check doesn't call it "unclear".
      newMemberOnboarding: /\/settings\/profile/.test(location.pathname) && /new_state=true/.test(location.search),
      challenge: /verifying you are human|checking your browser|attention required|just a moment/i.test(
        bodyText
      ),
      bodySample: bodyText.slice(0, 800),
      url: location.href,
    };
  });
}

async function readPostClickState(page) {
  return evaluateWithRetry(page, () => {
    const bodyText = document.body.innerText || "";
    const formQuestions = [...document.querySelectorAll("form label, form textarea, form input[type=text]")]
      .map((el) =>
        (el.closest("label")?.textContent || el.getAttribute("placeholder") || el.getAttribute("aria-label") || "").trim()
      )
      .filter(Boolean);
    return {
      hasMemberNav:
        !!document.querySelector('button[aria-label="Notifications"]') ||
        !!document.querySelector('button[aria-label="Direct messages"]') ||
        !!document.querySelector('button[aria-label="User menu options"]') ||
        [...document.querySelectorAll("[aria-label]")].some((el) =>
          /^(like the|comment on|bookmark)/i.test(el.getAttribute("aria-label") || "")
        ),
      pending:
        /pending approval|awaiting approval|request (has been )?(received|submitted)|thank you for (applying|your interest)/i.test(
          bodyText
        ),
      formQuestions: [...new Set(formQuestions)],
      onCheckout: /\/checkout/.test(location.pathname),
      // Confirmed live on two different hosts (otto-mates, sellyoursmarts):
      // a successful invitation-link join lands here ("Create a profile ...
      // This is how you'll appear in the community") before hasMemberNav's
      // icons exist yet -- a stronger, more direct joined signal than the
      // nav shortcut, not something to lump in with formQuestions/an
      // application form.
      newMemberOnboarding: /\/settings\/profile/.test(location.pathname) && /new_state=true/.test(location.search),
      url: location.href,
    };
  });
}

/** "Log in"/"Log In", "Sign in"/"Sign In" -- both capitalizations confirmed
 * live on different real Circle communities. Expands each candidate so
 * callers only need to write the one they happened to see. */
function withCaseVariant(text) {
  const alt = text.replace(/\b(in|up)\b/, (m) => m[0].toUpperCase() + m.slice(1));
  return alt === text ? [text] : [text, alt];
}

async function clickFirstMatch(page, texts) {
  const expanded = texts.flatMap(withCaseVariant);
  for (const text of expanded) {
    try {
      await page.click(`loc=role:button[name="${text}"]`);
      return text;
    } catch {
      // not present or ambiguous under that role selector -- try the next.
    }
  }
  for (const text of expanded) {
    try {
      await page.click(`text="${text}"`);
      return text;
    } catch {
      // fall through to the next candidate text
    }
  }
  return null;
}

/** Click Log in, then fill+submit whatever email/password form appears --
 * or, when Circle resolves the login silently through an existing
 * platform-wide session (confirmed live: clicking Log in on a *.circle.so
 * subdomain this account had never visited before completed with no form at
 * all), just notice that and stop there. Handles both a single
 * email+password form and an email-first "Continue, then password" flow.
 * Never guesses past an unrecognized shape -- returns { ok: false, reason }
 * instead, which the caller turns into a handoff. */
async function attemptLogin(page) {
  const clicked = await clickFirstMatch(page, ["Log in", "Sign in"]);
  if (!clicked) return { ok: false, reason: "could not click a Log in/Sign in control" };

  let sawForm = false;
  for (let i = 0; i < 6; i++) {
    await page.waitForTimeout(800);
    const check = await readPageState(page);
    if (!check.hasLogin) return { ok: true }; // resolved silently -- no form was ever shown
    const fields = await page.evaluate(
      ({ emailSelector, passwordSelector }) => ({
        hasEmail: !!document.querySelector(emailSelector),
        hasPassword: !!document.querySelector(passwordSelector),
      }),
      { emailSelector: EMAIL_SELECTOR, passwordSelector: PASSWORD_SELECTOR }
    );
    if (fields.hasEmail || fields.hasPassword) {
      sawForm = true;
      break;
    }
  }
  if (!sawForm) {
    return {
      ok: false,
      reason: "clicking Log in neither resolved automatically nor showed a form within 4.8s",
    };
  }

  // Up to 4 hops: confirmed live that a real Circle login can take several
  // clicks to reach an actual email/password form -- e.g. otto-mates (via an
  // invitation link): Log in -> lands on a sign-up-flavored page -> "Sign
  // in" pivots to a provider-choice screen -> "Sign in with an email"
  // finally reveals the fields.
  for (let round = 0; round < 4; round++) {
    const fields = await page.evaluate(
      ({ emailSelector, passwordSelector }) => ({
        hasEmail: !!document.querySelector(emailSelector),
        hasPassword: !!document.querySelector(passwordSelector),
      }),
      { emailSelector: EMAIL_SELECTOR, passwordSelector: PASSWORD_SELECTOR }
    );

    if (fields.hasPassword) {
      if (fields.hasEmail) {
        try {
          await page.fill(EMAIL_SELECTOR, loginEmail);
        } catch {
          return { ok: false, reason: "found an email field but could not fill it (ambiguous selector?)" };
        }
      }
      try {
        await page.fill(PASSWORD_SELECTOR, loginPassword);
      } catch {
        return { ok: false, reason: "found a password field but could not fill it (ambiguous selector?)" };
      }
      const submitted = await clickFirstMatch(page, ["Log in", "Sign in", "Continue", "Submit"]);
      if (!submitted) return { ok: false, reason: "filled credentials but found no submit control" };
      await page.waitForTimeout(2000);
      return { ok: true };
    }

    if (fields.hasEmail) {
      try {
        await page.fill(EMAIL_SELECTOR, loginEmail);
      } catch {
        return { ok: false, reason: "found an email field but could not fill it (ambiguous selector?)" };
      }
      // "Sign in" before "Continue"/"Next", deliberately: an email-first
      // Circle form some hosts show (confirmed live on otto-mates) defaults
      // to account *creation* -- its primary submit button says "Sign up" --
      // with a "Sign in" link off to the side for an existing account. Never
      // click "Sign up" here: this account already exists, so that would
      // attempt to register a duplicate.
      const advanced = await clickFirstMatch(page, ["Sign in", "Continue", "Next"]);
      if (!advanced) return { ok: false, reason: "filled email but found no Sign in/Continue control before a password step" };
      await page.waitForTimeout(1500);
      continue;
    }

    // Neither field yet: some hosts show a provider-choice screen first
    // (confirmed live: otto-mates' "Sign in with an email" button) before
    // the actual form appears.
    const pivoted = await clickFirstMatch(page, [
      "Sign in", "Sign in with an email", "Continue with email", "Log in with email",
    ]);
    if (!pivoted) return { ok: false, reason: "no recognizable email/password field, and no known pivot control, after clicking Log in" };
    await page.waitForTimeout(1500);
  }
  return { ok: false, reason: "no email/password form reached after 4 navigation hops" };
}

/** Everything that happens once we know the session is authenticated on this
 * host: decide whether membership already exists, or click Join and
 * classify the outcome. */
async function decideAndAct(page, state) {
  if (state.onCheckout) {
    // Confirmed live (mightyailab, a paid community): logged into Circle
    // platform-wide is NOT the same as being a paying member of this one
    // community -- landing on its own /checkout URL still showed member-nav
    // icons (Notifications etc.) despite no payment ever made. Never trust
    // the nav shortcut on a checkout URL. This codebase never pays, so
    // reaching a real checkout page (no $0 tier reachable without one) is
    // itself the confirmation there's no free path -- a normal, terminal
    // paid_skip, not something that needs a human to look at.
    const shot = await maybeScreenshot(page, "checkout-no-free-tier");
    emit("paid_skip", `Landed on a checkout/paywall URL (${state.url}) -- no free tier reachable.`, shot);
    return;
  }
  if (state.newMemberOnboarding) {
    const cookies = await captureSessionCookies(page);
    emit("joined", `Already on the new-member profile-setup page (${state.url}) -- no click needed.`, null, cookies);
    return;
  }
  if (state.hasMemberNav && !state.hasJoin) {
    const cookies = await captureSessionCookies(page);
    emit("joined", "Already authenticated with no Join control and member nav present -- no click needed.", null, cookies);
    return;
  }
  if (!state.hasJoin) {
    const shot = await maybeScreenshot(page, "unclear");
    await task.handOff();
    emit("unclear", `No Join control and no member nav detected (${state.url}) -- needs a human look.`, shot);
    return;
  }

  const clicked = await clickFirstMatch(page, JOIN_TEXTS);
  if (!clicked) {
    const shot = await maybeScreenshot(page, "join-control-not-clickable");
    await task.handOff();
    emit("unclear", "A Join-like control was detected in text but none of the known selectors could click it.", shot);
    return;
  }

  await page.waitForTimeout(2000);
  const after = await readPostClickState(page);
  const shot = await maybeScreenshot(page, "after-join");
  // onCheckout checked before hasMemberNav, deliberately -- same reasoning
  // as the guard above: being authenticated is not being a paying member,
  // and a checkout URL settles it either way.
  if (after.onCheckout) {
    emit("paid_skip", `Clicked "${clicked}" but landed on checkout (${after.url}) -- no free tier reachable.`, shot);
  } else if (after.hasMemberNav || after.newMemberOnboarding) {
    const cookies = await captureSessionCookies(page);
    emit(
      "joined",
      after.newMemberOnboarding
        ? `Clicked "${clicked}" -- landed on the new-member profile-setup page (${after.url}).`
        : `Clicked "${clicked}" -- member nav now present.`,
      shot,
      cookies
    );
  } else if (after.formQuestions.length > 0) {
    await task.handOff();
    emit(
      "application_form_detected",
      `Clicked "${clicked}"; a custom application form appeared (not auto-filled). Questions: ` +
        JSON.stringify(after.formQuestions.slice(0, 10)),
      shot
    );
  } else if (after.pending) {
    emit("pending_approval", `Clicked "${clicked}" -- community shows a pending-approval message.`, shot);
  } else {
    await task.handOff();
    emit("unclear", `Clicked "${clicked}" but the post-click state (${after.url}) didn't match a known pattern.`, shot);
  }
}

// takeOverTaskSpace, not taskSpace: the previous attempt in this same batch
// may have called task.handOff() (a login prompt, a challenge, ...) and left
// the space user-owned. Re-invoking `auto-join --space-id N` is itself the
// operator's explicit ask to resume it.
const task = await takeOverTaskSpace(spaceId);
const page = task.page("p1");

await page.goto(url, { timeout: 40_000 }); // default 15s wasn't enough for a genuinely slow custom domain (pythian-mystery-school)
await page.waitForLoadState();
await page.waitForTimeout(2000); // client-rendered SPA -- let the shell hydrate (1.2s wasn't always enough, confirmed live on awithub)

const state = await readPageState(page);

if (state.inviteOnlyGate) {
  const shot = await maybeScreenshot(page, "invite-only-gate");
  emit(
    "invite_skip",
    `Private community, no self-serve join -- "the admin may need to add you as a member" (${state.url}).`,
    shot
  );
} else if (state.subscriptionExpired) {
  const shot = await maybeScreenshot(page, "subscription-expired");
  emit(
    "subscription_expired_skip",
    `Community's own Circle subscription has lapsed (${state.url}) -- operator hasn't paid Circle, nothing to join.`,
    shot
  );
} else if (state.challenge) {
  const shot = await maybeScreenshot(page, "challenge");
  await task.handOff();
  emit("challenge_stop", "Cloudflare/human-verification challenge detected -- stopped, no bypass attempted.", shot);
} else if (state.hasLogin) {
  if (!(loginEmail && loginPassword)) {
    const shot = await maybeScreenshot(page, "needs-login");
    await task.handOff();
    emit(
      "needs_login",
      `Not authenticated on this host (${state.url}) and no CIRCLE_EMAIL/CIRCLE_PASSWORD configured -- log in manually, then resume.`,
      shot
    );
  } else {
    const loginResult = await attemptLogin(page);
    if (!loginResult.ok) {
      const shot = await maybeScreenshot(page, "login-failed");
      await task.handOff();
      emit("needs_login", `Automatic login failed (${loginResult.reason}) -- log in manually, then resume.`, shot);
    } else {
      const afterLogin = await readPageState(page);
      if (afterLogin.challenge) {
        const shot = await maybeScreenshot(page, "challenge-after-login");
        await task.handOff();
        emit("challenge_stop", "A challenge appeared right after login -- stopped, no bypass attempted.", shot);
      } else if (afterLogin.hasLogin) {
        const shot = await maybeScreenshot(page, "still-logged-out");
        await task.handOff();
        emit(
          "needs_login",
          "Submitted login but the host still shows Log in (wrong password on this host? 2FA?) -- resolve manually.",
          shot
        );
      } else {
        await decideAndAct(page, afterLogin);
      }
    }
  }
} else {
  await decideAndAct(page, state);
}
