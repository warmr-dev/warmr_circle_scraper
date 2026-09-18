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
// Membership is decided by asking Circle, not by reading icons. Circle's own
// /internal_api/pundit_users answers, from inside the page (confirmed live
// 2026-09-18 on the same host in three states):
//   logged out            -> no current_user
//   logged in, not member -> current_user, no current_community_member
//   member                -> current_community_member (+ current_user.email,
//                            compared here against the account we run as)
// and /internal_api/spaces answers 400 "Please confirm before proceeding"
// until the new-member "Create a profile" step is saved -- so "joined" is only
// emitted once that call returns 200 as a member. The earlier icon heuristic
// called two joins done that were stuck on that step, unreadable.
//
// Circle sessions from *.circle.so subdomains do NOT carry over to a custom
// domain (confirmed live), so a login prompt on a new host is expected and
// handled here. What still stops and hands the browser back to the operator:
// no credentials, an unrecognized login form, a Cloudflare/human-verification
// challenge (no bypass attempted, ever), a different account already signed
// in, an application form, a profile field the bot may not answer.
//
// Emits exactly one line, "EGO_JOIN_RESULT=<json>", as the last line of
// output: { status, detail, screenshot, cookies, form }. `status` is one of:
//   joined, paid_skip, pending_approval, subscription_expired_skip,
//   invite_skip, dead_host, external_login                -- terminal, DB-recorded
//   profile_form        -- required profile questions; `form.fields` lists them.
//                          The caller resolves answers and runs again with them.
//   needs_login, challenge_stop, application_form_detected, unclear,
//   nav_failed, wrong_account, profile_incomplete, email_code_needed,
//   driver_error                                           -- handoff
// `cookies` (only on "joined") is this host's session cookies, for
// circle_leads/web/replay_store.py.

const spaceId = __EGO_JOIN_SPACE_ID__;
const url = __EGO_JOIN_URL__;
// Credentials go only to Circle: the community's own host (checked as a
// Circle community by the join-type probe before it was ever queued) or a
// *.circle.so host. A community's own identity provider is neither, however
// Circle-branded or Circle-linked its page looks -- confirmed live
// 2026-09-21: aimarketerhq's "Sign up" led to auth0.aimarketerhq.com, and an
// asset-based "is this Circle?" check waved the credentials through there.
const communityHost = new URL(url).hostname.toLowerCase();
const screenshotDir = __EGO_JOIN_SCREENSHOT_DIR__ || "";
const loginEmail = __EGO_JOIN_EMAIL__;
const loginPassword = __EGO_JOIN_PASSWORD__;
// null on a first pass; { "<circle profile field id>": value } on the second
// pass, after the caller looked the questions up in its answer database.
const profileAnswers = __EGO_JOIN_ANSWERS__;

// Button texts, matched whole and case-insensitively. English first (every
// Circle community we met), then the languages Circle ships its UI in --
// ulule's French "Rejoindre"/"Se connecter" left the bot blind (2026-09-18).
const JOIN_TEXTS = [
  "Join", "Join for free", "Join now", "Join community", "Sign up", "Sign up for free", "Get access",
  "Request to join", "Accept invitation", "Register for Free", "Register",
  // A community with public space previews shows a per-space CTA instead of a
  // page-level one (confirmed live: the-technical-freelancer-academy); for a
  // visitor who isn't a member it starts the community join itself
  // (agencybuilders, 2026-09-18).
  "Join space",
  // A discover.circle.so listing of a paid community only says "Buy", which
  // goes to /checkout -- caught by the onCheckout guard as paid_skip.
  "Buy",
  "Rejoindre", "Rejoindre la communauté", "S'inscrire", "Demander à rejoindre",
  "Beitreten", "Jetzt beitreten", "Registrieren", "Mitglied werden",
  "Unirse", "Únete", "Unirme", "Registrarse", "Solicitar unirse",
  "Participar", "Inscrever-se", "Cadastre-se", "Entrar na comunidade",
  "Unisciti", "Iscriviti", "Dołącz", "Zarejestruj się",
  "Word lid", "Lid worden", "Registreren",
];
const LOGIN_TEXTS = [
  "Log in", "Sign in", "Login",
  "Se connecter", "Connexion", "Anmelden", "Einloggen", "Iniciar sesión",
  "Iniciar sessão", "Entrar", "Accedi", "Zaloguj się", "Inloggen",
];
const LOGIN_SUBMIT_TEXTS = [
  ...LOGIN_TEXTS, "Continue", "Submit",
  "Continuer", "Weiter", "Continuar", "Continua", "Dalej", "Doorgaan",
];
const EMAIL_STEP_TEXTS = [
  "Sign in", "Log in", "Continue", "Next",
  "Se connecter", "Continuer", "Suivant", "Anmelden", "Weiter",
  "Iniciar sesión", "Continuar", "Siguiente", "Accedi", "Continua", "Avanti",
  "Zaloguj się", "Dalej", "Inloggen", "Doorgaan",
];
const LOGIN_PIVOT_TEXTS = [
  "Sign in", "Log in", "Sign in with an email", "Continue with email", "Log in with email",
  "Se connecter", "Anmelden", "Iniciar sesión", "Accedi", "Zaloguj się", "Inloggen",
];
const PROFILE_CONTINUE_TEXTS = [
  "Continue", "Save", "Save and continue", "Next", "Done", "Finish",
  "Continuer", "Enregistrer", "Suivant", "Terminer",
  "Weiter", "Speichern", "Fertig", "Continuar", "Guardar", "Siguiente",
  "Continua", "Salva", "Avanti", "Dalej", "Zapisz", "Doorgaan", "Opslaan",
];
const EMAIL_SELECTOR =
  "input[type=email], input[name*=email i], input[autocomplete=email], input[autocomplete=username]";
const PASSWORD_SELECTOR = "input[type=password]";
// Mirrors circle_leads/scraper/member_api_reader.py::SESSION_COOKIE_NAMES --
// the only cookies that carry a member session (cf_clearance etc deliberately
// excluded there too: not required by /internal_api, and IP/device-bound).
const SESSION_COOKIE_NAMES = ["_circle_session", "remember_user_token", "user_session_identifier"];

let emitted = false;
let profileStepDone = false; // set only when the bot itself saved "Create a profile"
function emit(status, detail, screenshot, cookies, form) {
  emitted = true;
  console.log(
    "EGO_JOIN_RESULT=" +
      JSON.stringify({
        status, detail, screenshot: screenshot || null, cookies: cookies || null, form: form || null,
      })
  );
}

/** Only ever called right before emitting "joined" -- confirmed live
 * (page.cdp("Network.getCookies", {}) with no `urls` scopes to the current
 * page) that this returns exactly the current host's cookies. Never throws. */
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
  try {
    const path = `${screenshotDir}/${tag}-${Date.now()}.png`;
    await page.screenshot({ path });
    return path;
  } catch {
    return null; // a failed screenshot must never turn into a worse outcome
  }
}

async function handOffAndEmit(page, status, detail, tag) {
  const shot = await maybeScreenshot(page, tag || status);
  try {
    await task.handOff();
  } catch {
    // already user-owned
  }
  emit(status, detail, shot);
}

/** Wait until a client-rendered page has actually rendered. A fixed 2 s was
 * not enough: entrepreneur-bootcamp was judged while it still showed only a
 * spinner (2026-09-18). Settled = load done, network quiet, some text on the
 * page, nothing marked busy, and the DOM unchanged between two polls. */
async function waitForSettled(page, maxMs = 15000) {
  const deadline = Date.now() + maxMs;
  try {
    await page.waitForLoadState("networkidle", { timeout: maxMs, idleMs: 800 });
  } catch {
    // a chatty page never goes fully idle -- the DOM check below still applies
  }
  let last = null;
  while (Date.now() < deadline) {
    let snap = null;
    try {
      snap = await page.evaluate(() => {
        const text = (document.body && document.body.innerText) || "";
        return {
          ready: document.readyState,
          len: text.trim().length,
          nodes: document.getElementsByTagName("*").length,
          busy: !!document.querySelector('[aria-busy="true"], [role="progressbar"]'),
        };
      });
    } catch {
      snap = null; // mid-navigation: no document yet
    }
    if (snap && last && snap.ready === "complete" && snap.len > 40 && !snap.busy &&
        snap.len === last.len && snap.nodes === last.nodes) {
      return true;
    }
    last = snap;
    await page.waitForTimeout(600);
  }
  return false;
}

/** A page mid-navigation can briefly have no document/body -- evaluate() then
 * throws instead of returning a value (confirmed live on otto-mates' and
 * sellyoursmarts' invitation-link redirects). Retry with a growing wait. */
async function evaluateWithRetry(page, fn, arg) {
  const isTransient = (err) => /reading 'innerText'|reading 'body'|Cannot read properties of null|Execution context was destroyed/.test(String(err));
  // ego-browser rejects an explicit `undefined` argument ("page.evaluate
  // argument must be JSON-serializable", live 2026-09-18) -- omit it instead.
  const run = () => (arg === undefined ? page.evaluate(fn) : page.evaluate(fn, arg));
  for (const delayMs of [800, 1600, 2400]) {
    try {
      return await run();
    } catch (err) {
      if (!isTransient(err)) throw err;
      await page.waitForTimeout(delayMs);
    }
  }
  return run();
}

async function currentUrl(page) {
  try {
    return String(await page.url());
  } catch {
    return "";
  }
}

async function fetchJson(page, path) {
  try {
    const r = await page.fetch(path, { timeout: 15000 });
    let json = null;
    try {
      json = JSON.parse(r.body);
    } catch {
      json = null;
    }
    return { status: r.status, json };
  } catch {
    return { status: 0, json: null };
  }
}

/** Who Circle says we are on this host. api=false when the page isn't a
 * Circle community at all (a marketing site in front of it, a dead page). */
async function memberState(page) {
  const { status, json } = await fetchJson(page, "/internal_api/pundit_users");
  if (status !== 200 || !json || typeof json !== "object" || !("current_community" in json)) {
    return { api: false, status };
  }
  const user = json.current_user || null;
  const member = json.current_community_member || null;
  const email = user ? String(user.email || "").toLowerCase() : "";
  const expected = String(loginEmail || "").toLowerCase();
  return {
    api: true,
    loggedIn: !!user,
    otherAccount: !!user && !!expected && !!email && email !== expected,
    isMember: !!member,
    profileConfirmed: member ? !!member.profile_confirmed_at : false,
  };
}

/** Can this session read the community's API? 400 "Please confirm before
 * proceeding" means the new-member profile step is still open. */
async function apiReadable(page) {
  const { status, json } = await fetchJson(page, "/internal_api/spaces");
  const message = json && typeof json === "object" && !Array.isArray(json) ? json.message || "" : "";
  return {
    ok: status === 200 && !(json && json.success === false),
    status,
    profilePending: status === 400 && /please confirm/i.test(message),
    message,
  };
}

async function readPageState(page) {
  return evaluateWithRetry(page, ({ joinTexts, loginTexts }) => {
    const norm = (t) => (t || "").replace(/\s+/g, " ").trim().toLowerCase();
    const joinSet = new Set(joinTexts.map(norm));
    const loginSet = new Set(loginTexts.map(norm));
    const clickable = [...document.querySelectorAll("button, a, [role=button]")]
      .map((el) => norm(el.textContent))
      .filter(Boolean);
    const bodyText = document.body.innerText || "";
    return {
      // Confirmed live (agency-mavericks): a private/invite-only community
      // shows "This is a private community -- it doesn't look like you have
      // access ... the admin may need to add you as a member".
      inviteOnlyGate:
        /this is a private community/i.test(bodyText) &&
        /doesn.?t look like you have access|admin may need to add you/i.test(bodyText),
      hasLogin: clickable.some((t) => loginSet.has(t)),
      hasJoin: clickable.some((t) => joinSet.has(t)),
      onCheckout: /\/checkout(\/|$)/.test(location.pathname),
      // Confirmed live (founderscupid): Circle redirects here when the
      // community operator's own Circle subscription has lapsed.
      subscriptionExpired: /\/subscription_expired(\/|$)/.test(location.pathname),
      newMemberOnboarding: /\/settings\/profile/.test(location.pathname) && /new_state=true/.test(location.search),
      challenge: /verifying you are human|checking your browser|attention required|just a moment/i.test(bodyText),
      url: location.href,
    };
  }, { joinTexts: JOIN_TEXTS, loginTexts: LOGIN_TEXTS });
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
      pending:
        /pending approval|awaiting approval|request (has been )?(received|submitted)|thank you for (applying|your interest)/i.test(
          bodyText
        ),
      formQuestions: [...new Set(formQuestions)],
      onCheckout: /\/checkout/.test(location.pathname),
      newMemberOnboarding: /\/settings\/profile/.test(location.pathname) && /new_state=true/.test(location.search),
      url: location.href,
    };
  });
}

/** Click the first visible button/link whose whole text matches one of
 * `texts`, ignoring case and extra whitespace. Returns the text clicked. */
async function clickFirstMatch(page, texts) {
  const hit = await evaluateWithRetry(page, (wanted) => {
    const norm = (t) => (t || "").replace(/\s+/g, " ").trim().toLowerCase();
    document.querySelectorAll("[data-warmr-click]").forEach((el) => el.removeAttribute("data-warmr-click"));
    const visible = (el) => !!(el.offsetParent || el.getClientRects().length) && !el.disabled;
    const candidates = [...document.querySelectorAll("button, a, [role=button], input[type=submit]")].filter(visible);
    for (const text of wanted) {
      const t = norm(text);
      const el = candidates.find((c) => norm(c.textContent || c.value) === t);
      if (el) {
        el.setAttribute("data-warmr-click", "1");
        return text;
      }
    }
    return null;
  }, texts);
  if (!hit) return null;
  try {
    await page.click('css=[data-warmr-click="1"]');
    return hit;
  } catch {
    return null;
  }
}

/** Is the page in front of us Circle itself -- the community's own host or a
 * *.circle.so host? Nothing else qualifies. A community can sign people in
 * through its own website: ecommerce sent the bot to login.ecommerce.pl and
 * ulule to ulule.com (2026-09-18) -- pages that are not Circle and must never
 * receive the Circle password. Asking the page whether it "answers Circle's
 * API" is not a substitute for the host check: see communityHost. */
async function isCirclePage(page) {
  let host = "";
  try {
    host = new URL(await currentUrl(page)).hostname.toLowerCase();
  } catch {
    return false;
  }
  return host === communityHost || host === "circle.so" || host.endsWith(".circle.so");
}

class ForeignLoginError extends Error {}

/** The only place credentials are typed. Refuses unless the page is Circle. */
async function fillCredential(page, selector, value) {
  if (!(await isCirclePage(page))) {
    let host = "";
    try {
      host = new URL(await currentUrl(page)).hostname;
    } catch {
      host = "an unknown page";
    }
    throw new ForeignLoginError(host);
  }
  await page.fill(selector, value);
}

/** Click Log in, then fill+submit whatever email/password form appears --
 * or, when Circle resolves the login silently through an existing
 * platform-wide session, just notice that and stop there. Handles a single
 * email+password form and an email-first "Continue, then password" flow.
 * Never guesses past an unrecognized shape. */
async function attemptLogin(page, { onLoginPage = false } = {}) {
  if (!onLoginPage) {
    const clicked = await clickFirstMatch(page, LOGIN_TEXTS);
    if (!clicked) return { ok: false, reason: "could not click a Log in/Sign in control" };
  }

  const fieldsNow = () =>
    page.evaluate(
      ({ emailSelector, passwordSelector }) => ({
        hasEmail: !!document.querySelector(emailSelector),
        hasPassword: !!document.querySelector(passwordSelector),
      }),
      { emailSelector: EMAIL_SELECTOR, passwordSelector: PASSWORD_SELECTOR }
    );

  let sawForm = false;
  for (let i = 0; i < 8; i++) {
    await page.waitForTimeout(800);
    const ms = await memberState(page);
    if (ms.api && ms.loggedIn) return { ok: true }; // resolved silently -- no form was ever shown
    const fields = await fieldsNow().catch(() => ({ hasEmail: false, hasPassword: false }));
    if (fields.hasEmail || fields.hasPassword) {
      sawForm = true;
      break;
    }
  }
  if (!sawForm) {
    return { ok: false, reason: "clicking Log in neither signed in nor showed a form within 6.4s" };
  }

  // Up to 4 hops: a real Circle login can take several clicks to reach the
  // fields (confirmed live on otto-mates: Log in -> a sign-up-flavored page
  // -> "Sign in" -> provider choice -> "Sign in with an email").
  for (let round = 0; round < 4; round++) {
    const fields = await fieldsNow();
    if (fields.hasPassword) {
      if (fields.hasEmail) {
        try {
          await fillCredential(page, EMAIL_SELECTOR, loginEmail);
        } catch (err) {
          if (err instanceof ForeignLoginError) return { ok: false, foreign: err.message };
          return { ok: false, reason: "found an email field but could not fill it (ambiguous selector?)" };
        }
      }
      try {
        await fillCredential(page, PASSWORD_SELECTOR, loginPassword);
      } catch (err) {
        if (err instanceof ForeignLoginError) return { ok: false, foreign: err.message };
        return { ok: false, reason: "found a password field but could not fill it (ambiguous selector?)" };
      }
      const submitted = await clickFirstMatch(page, LOGIN_SUBMIT_TEXTS);
      if (!submitted) return { ok: false, reason: "filled credentials but found no submit control" };
      await page.waitForTimeout(2000);
      await waitForSettled(page, 12000);
      return { ok: true };
    }
    if (fields.hasEmail) {
      try {
        await fillCredential(page, EMAIL_SELECTOR, loginEmail);
      } catch (err) {
        if (err instanceof ForeignLoginError) return { ok: false, foreign: err.message };
        return { ok: false, reason: "found an email field but could not fill it (ambiguous selector?)" };
      }
      // "Sign in" before "Continue"/"Next", deliberately: an email-first form
      // some hosts show defaults to account *creation* ("Sign up") with a
      // "Sign in" link off to the side. Never click "Sign up" here.
      const advanced = await clickFirstMatch(page, EMAIL_STEP_TEXTS);
      if (!advanced) return { ok: false, reason: "filled email but found no Sign in/Continue control before a password step" };
      await page.waitForTimeout(1500);
      continue;
    }
    const pivoted = await clickFirstMatch(page, LOGIN_PIVOT_TEXTS);
    if (!pivoted) return { ok: false, reason: "no recognizable email/password field, and no known pivot control, after clicking Log in" };
    await page.waitForTimeout(1500);
  }
  return { ok: false, reason: "no email/password form reached after 4 navigation hops" };
}

/** Submit controls of a registration form, as opposed to a login form. "Sign
 * up" is deliberate here and forbidden in attemptLogin: there the account
 * already exists on Circle, here this host has none yet. */
const SIGNUP_SUBMIT_TEXTS = ["Sign up", "Create account", "Register", "Join", "Continue", "Submit"];

/** The shape of the form that appeared after clicking Join.
 *
 * A form asking for nothing but an email and a password is the registration
 * step of a free community: the answers are the credentials this batch
 * already holds, and filling them is the same act as filling the login form
 * above. Anything else -- a name, "why do you want to join?", a dropdown --
 * is a question for the operator, so the check is deliberately narrow.
 * Checkboxes (terms) are counted but never ticked: a required one just fails
 * the submit, which hands off like before. */
async function readSignupFormShape(page) {
  return page.evaluate(
    ({ emailSelector }) => {
      const form = [...document.querySelectorAll("form")].find((f) =>
        f.querySelector("input[type=password]")
      );
      if (!form) return { fillable: false, reason: "no form holds a password field" };
      const visible = (el) => {
        const style = window.getComputedStyle(el);
        return style.display !== "none" && style.visibility !== "hidden" && el.type !== "hidden";
      };
      const kinds = [...form.querySelectorAll("input, textarea, select")]
        .filter(visible)
        .filter((el) => !["submit", "button", "image", "reset"].includes(el.type))
        .map((el) => {
          if (el.tagName !== "INPUT") return el.tagName.toLowerCase();
          if (el.type === "password") return "password";
          if (el.matches(emailSelector)) return "email";
          return (el.type || "text").toLowerCase();
        });
      const asked = [...new Set(kinds.filter((k) => !["email", "password", "checkbox"].includes(k)))];
      return {
        fillable: asked.length === 0 && kinds.includes("password"),
        hasEmail: kinds.includes("email"),
        reason: asked.length ? `the form also asks for ${asked.join(", ")}` : "",
      };
    },
    { emailSelector: EMAIL_SELECTOR }
  );
}

/** Fill and submit that registration form when it is nothing but email and
 * password, typing only through fillCredential. { done: true } once it has
 * emitted an outcome; { done: false, reason } for every other shape, with
 * nothing emitted, and the caller hands the browser to the operator. A
 * membership is still confirmed by finishMembership, not by the page. */
async function tryCompleteSignupForm(page, clicked) {
  if (!loginEmail || !loginPassword) return { done: false, reason: "no credentials configured" };
  const shape = await readSignupFormShape(page);
  if (!shape.fillable) return { done: false, reason: shape.reason || "unrecognized form" };
  try {
    if (shape.hasEmail) await fillCredential(page, EMAIL_SELECTOR, loginEmail);
    await fillCredential(page, PASSWORD_SELECTOR, loginPassword);
  } catch (err) {
    if (err instanceof ForeignLoginError) {
      return { done: false, reason: `the form is on ${err.message}, not the community's host or circle.so -- credentials withheld` };
    }
    return { done: false, reason: "could not fill the sign-up form (ambiguous selector?)" };
  }
  const submitted = await clickFirstMatch(page, SIGNUP_SUBMIT_TEXTS);
  if (!submitted) return { done: false, reason: "filled the sign-up form but found no submit control" };
  await page.waitForTimeout(2500);
  await waitForSettled(page, 12000);
  const after = await readPostClickState(page);
  if (after.onCheckout) {
    emit("paid_skip", `Clicked "${clicked}", filled the email+password sign-up form, landed on checkout (${after.url}).`,
      await maybeScreenshot(page, "after-signup-form"));
    return { done: true };
  }
  const ms = await memberState(page);
  if (ms.otherAccount) {
    await handOffAndEmit(page, "wrong_account", "After the sign-up form a different Circle account is signed in -- stopped.");
    return { done: true };
  }
  if (ms.isMember || after.newMemberOnboarding) {
    await finishMembership(page, `Clicked "${clicked}" and filled the email+password sign-up form`);
    return { done: true };
  }
  if (after.pending) {
    emit("pending_approval", `Clicked "${clicked}", filled the sign-up form -- community shows a pending-approval message.`,
      await maybeScreenshot(page, "after-signup-form"));
    return { done: true };
  }
  return { done: false, reason: `submitted the sign-up form but the page (${after.url}) didn't match a known pattern` };
}

/** Open the target, sorting failures into a dead host (the name no longer
 * resolves -- founders-run-club redirects to a domain that is gone) versus a
 * load that was merely interrupted. ERR_ABORTED (bumbleb's invitation link,
 * 2026-09-18) usually means a redirect replaced the load: carry on if a real
 * page is showing, else retry once. */
async function navigate(page, target) {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      await page.goto(target, { timeout: 40_000 }); // 15s wasn't enough for a slow custom domain (pythian-mystery-school)
      return null;
    } catch (err) {
      const msg = String((err && err.message) || err);
      const code = (msg.match(/net::[A-Z_]+/) || [msg.slice(0, 120)])[0];
      const now = await currentUrl(page);
      if (/ERR_NAME_NOT_RESOLVED/.test(msg)) {
        const where = now && !/^(about:blank|chrome-error:)/.test(now) && now !== target ? ` (redirect ended at ${now})` : "";
        return { status: "dead_host", detail: `Host no longer resolves: ${target}${where} -- ${code}.` };
      }
      if (/ERR_ABORTED/.test(msg)) {
        await page.waitForTimeout(3000);
        const after = await currentUrl(page);
        if (after && !/^(about:blank|chrome-error:)/.test(after)) return null;
        continue;
      }
      if (attempt === 0 && /timeout|timed out|ERR_TIMED_OUT|ERR_CONNECTION/i.test(msg)) continue;
      return { status: "nav_failed", detail: `Could not open ${target}: ${code}.` };
    }
  }
  return { status: "nav_failed", detail: `Could not open ${target}: the load kept aborting after a retry.` };
}

// --- the new-member profile step ------------------------------------------------

/** Circle's description of the "Create a profile" form: top-level name and
 * time_zone plus profile_fields[] (id, label, field_type, required, choices,
 * platform_field, and this member's current value). Field order is the index
 * in the form's input names: community_member_profile_fields_attributes.<i>.*.
 * Confirmed live 2026-09-18 (agencybuilders, freelancemvp). */
async function readProfileForm(page) {
  const { status, json } = await fetchJson(page, "/internal_api/signup/profile");
  if (status !== 200 || !json || !Array.isArray(json.profile_fields)) return null;
  const hasValue = (cmpf) => {
    if (!cmpf || typeof cmpf !== "object") return false;
    return Object.entries(cmpf).some(([k, v]) => {
      if (/^(id|created_at|updated_at|profile_field_id|community_member_id)$/.test(k)) return false;
      if (v === null || v === undefined || v === "" || v === false) return false;
      if (Array.isArray(v)) return v.length > 0;
      if (typeof v === "object") return Object.keys(v).length > 0;
      return true;
    });
  };
  const choiceText = (c) => (c && typeof c === "object" ? c.value ?? c.label ?? c.name ?? c.text ?? "" : String(c));
  const missing = json.profile_fields
    .map((f, index) => ({ f, index }))
    .filter(({ f }) => f.required && !hasValue(f.community_member_profile_field))
    .map(({ f, index }) => ({
      id: String(f.id),
      index,
      label: String(f.label || f.key || `field ${index}`),
      field_type: String(f.field_type || "text"),
      required: true,
      choices: (f.choices || []).map(choiceText).filter((c) => String(c).trim()),
      description: f.description || null,
      platform_field: !!f.platform_field,
    }));
  return { hasName: !!String(json.name || "").trim(), hasTimeZone: !!String(json.time_zone || "").trim(), missing };
}

/** Put one answer into field #index. Only plain inputs, textareas, native
 * selects, checkboxes and radios -- anything custom goes to a human. */
async function fillProfileField(page, index, value) {
  const prefix = `community_member_profile_fields_attributes.${index}.`;
  const els = await evaluateWithRetry(page, (p) =>
    [...document.querySelectorAll(`[name^="${p}"]`)].map((e) => ({
      tag: e.tagName.toLowerCase(),
      type: (e.getAttribute("type") || "").toLowerCase(),
      name: e.getAttribute("name"),
      value: e.getAttribute("value"),
      label: (e.closest("label")?.textContent || "").trim(),
      visible: !!(e.offsetParent || e.getClientRects().length),
    })), prefix);
  const vis = els.filter((e) => e.visible);
  const css = (name) => `css=[name="${name}"]`;

  if (typeof value === "boolean") {
    const box = vis.find((e) => e.type === "checkbox");
    if (!box) return false;
    const checked = await page.evaluate((n) => document.querySelector(`[name="${n}"]`).checked, box.name);
    if (checked !== value) await page.click(css(box.name));
    return true;
  }
  const select = vis.find((e) => e.tag === "select");
  if (select) {
    await page.selectOption(css(select.name), value);
    const shown = await page.evaluate((n) => {
      const s = document.querySelector(`[name="${n}"]`);
      return [...s.selectedOptions].map((o) => o.textContent.trim());
    }, select.name);
    const want = (Array.isArray(value) ? value : [value]).map(String);
    return want.every((w) => shown.includes(w));
  }
  const radios = vis.filter((e) => e.type === "radio");
  if (radios.length) {
    const target = radios.find((r) => r.label === String(value) || r.value === String(value));
    if (!target) return false;
    await page.click(`css=[name="${target.name}"][value="${target.value}"]`);
    return true;
  }
  const box = vis.find((e) => e.tag === "textarea" || (e.tag === "input" && ["", "text", "url", "number", "email", "tel"].includes(e.type)));
  if (box && !Array.isArray(value)) {
    await page.fill(css(box.name), String(value));
    return true;
  }
  return false;
}

/** Circle's "Check your inbox" page: a one-time code was emailed to the
 * account and must be typed in (seen for the test account on
 * entrepreneur-bootcamp, 2026-09-18). The bot has no inbox access. */
async function onEmailCodePage(page) {
  try {
    return await page.evaluate(() =>
      /check your inbox|verification code|enter the code we sent/i.test((document.body && document.body.innerText) || ""));
  } catch {
    return false;
  }
}

/** One pass over the profile step. Returns true to check again, false when
 * it has already emitted (a handoff, or profile_form for the caller). */
async function completeProfileStep(page) {
  if (await onEmailCodePage(page)) {
    await handOffAndEmit(page, "email_code_needed",
      "Circle emailed a verification code to this account and asks for it before the membership is usable -- " +
        "enter it in this browser profile, then run again.");
    return false;
  }
  const here = await currentUrl(page);
  if (!/\/settings\/profile/.test(here)) {
    const origin = new URL(here || url).origin;
    const nav = await navigate(page, `${origin}/settings/profile?new_state=true`);
    if (nav) {
      await handOffAndEmit(page, "profile_incomplete", `Member, but the profile page did not open: ${nav.detail}`);
      return false;
    }
    await waitForSettled(page);
  }
  const form = await readProfileForm(page);
  if (!form) {
    await handOffAndEmit(page, "profile_incomplete", "Member, but Circle did not describe the profile form (/internal_api/signup/profile).");
    return false;
  }
  if (!form.hasName || !form.hasTimeZone) {
    await handOffAndEmit(page, "profile_incomplete",
      `Profile step: ${!form.hasName ? "Full name" : "Timezone"} is empty and is not something the bot fills.`);
    return false;
  }
  if (form.missing.length) {
    if (!profileAnswers) {
      // Hand the questions to the caller's answer database; it runs us again.
      emit("profile_form", `Profile step asks ${form.missing.length} required question(s).`,
        await maybeScreenshot(page, "profile-form"), null, { fields: form.missing });
      return false;
    }
    for (const f of form.missing) {
      const value = profileAnswers[f.id];
      if (value === undefined || value === null) {
        await handOffAndEmit(page, "profile_incomplete", `Profile step: no answer for required "${f.label}".`);
        return false;
      }
      let ok = false;
      try {
        ok = await fillProfileField(page, f.index, value);
      } catch {
        ok = false;
      }
      if (!ok) {
        await handOffAndEmit(page, "profile_incomplete",
          `Profile step: could not fill required "${f.label}" (${f.field_type}) -- custom widget?`);
        return false;
      }
    }
  }
  const clicked = await clickFirstMatch(page, PROFILE_CONTINUE_TEXTS);
  if (!clicked) {
    if (await onEmailCodePage(page)) {
      await handOffAndEmit(page, "email_code_needed",
        "Circle emailed a verification code to this account and asks for it before the membership is usable -- " +
          "enter it in this browser profile, then run again.");
    } else {
      await handOffAndEmit(page, "profile_incomplete", "Profile step: no Continue/Save button found.");
    }
    return false;
  }
  profileStepDone = true;
  await page.waitForTimeout(1500);
  await waitForSettled(page);
  return true;
}

/** We are a member: make sure the membership is usable, then report it. */
async function finishMembership(page, how) {
  for (let pass = 0; pass < 4; pass++) {
    const readable = await apiReadable(page);
    if (readable.ok) {
      const cookies = await captureSessionCookies(page);
      emit("joined",
        `${how} -- member, and the community API answers` +
          (profileStepDone ? " (the bot saved the new-member profile step)." : "."),
        await maybeScreenshot(page, "joined"), cookies);
      return;
    }
    if (!readable.profilePending) {
      // Some onboarding pages (welcome, pick spaces) sit between the profile
      // and the community; a Continue-type button moves past them.
      const moved = pass < 3 ? await clickFirstMatch(page, PROFILE_CONTINUE_TEXTS) : null;
      if (moved) {
        await page.waitForTimeout(1500);
        await waitForSettled(page);
        continue;
      }
      await handOffAndEmit(page, "unclear",
        `Member, but the community API answers ${readable.status}${readable.message ? ` "${readable.message}"` : ""}.`);
      return;
    }
    if (!(await completeProfileStep(page))) return;
  }
  await handOffAndEmit(page, "profile_incomplete", "Profile step still open after 4 passes.");
}

// --- the flow ---------------------------------------------------------------------

/** Stops for a page that should not be joined at all; true when it emitted. */
async function stoppedByGate(page, state) {
  if (state.inviteOnlyGate) {
    emit("invite_skip", `Private community, no self-serve join -- "the admin may need to add you as a member" (${state.url}).`,
      await maybeScreenshot(page, "invite-only-gate"));
    return true;
  }
  if (state.subscriptionExpired) {
    emit("subscription_expired_skip", `Community's own Circle subscription has lapsed (${state.url}) -- nothing to join.`,
      await maybeScreenshot(page, "subscription-expired"));
    return true;
  }
  if (state.challenge) {
    await handOffAndEmit(page, "challenge_stop", "Cloudflare/human-verification challenge detected -- stopped, no bypass attempted.", "challenge");
    return true;
  }
  if (state.onCheckout) {
    // Signed in is not paying: a checkout URL settles it (confirmed live on mightyailab).
    emit("paid_skip", `Landed on a checkout/paywall URL (${state.url}) -- no free tier reachable.`,
      await maybeScreenshot(page, "checkout-no-free-tier"));
    return true;
  }
  return false;
}

/** Sign in with this account's credentials. True when signed in as it; false
 * when it emitted a handoff. With no Log in control on the page (a public
 * feed that shows none: entrepreneur-bootcamp, 2026-09-18) it opens Circle's
 * own sign-in page on this host instead. */
async function signIn(page, { hasLoginControl = true } = {}) {
  if (!(loginEmail && loginPassword)) {
    await handOffAndEmit(page, "needs_login", "Not signed in and no credentials configured -- log in manually, then resume.");
    return false;
  }
  let onLoginPage = false;
  if (!hasLoginControl) {
    const origin = new URL((await currentUrl(page)) || url).origin;
    const nav = await navigate(page, `${origin}/users/sign_in`);
    if (nav) {
      await handOffAndEmit(page, "needs_login", `Signed out, no Log in control, and ${origin}/users/sign_in did not open: ${nav.detail}`);
      return false;
    }
    await waitForSettled(page);
    onLoginPage = true;
  }
  const login = await attemptLogin(page, { onLoginPage });
  if (!login.ok && login.foreign) {
    emit("external_login",
      `The community signs in through its own site (${login.foreign}), not Circle -- it needs an account there. ` +
        "The bot never types the Circle login outside Circle.",
      await maybeScreenshot(page, "external-login"));
    return false;
  }
  if (!login.ok) {
    await handOffAndEmit(page, "needs_login", `Automatic login failed (${login.reason}) -- log in manually, then resume.`, "login-failed");
    return false;
  }
  await waitForSettled(page);
  const state = await readPageState(page);
  if (state.challenge) {
    await handOffAndEmit(page, "challenge_stop", "A challenge appeared right after login -- stopped, no bypass attempted.", "challenge-after-login");
    return false;
  }
  const ms = await memberState(page);
  if (ms.otherAccount) {
    await handOffAndEmit(page, "wrong_account", "After login a different Circle account is signed in -- not joining as it.");
    return false;
  }
  if (ms.api ? !ms.loggedIn : state.hasLogin) {
    await handOffAndEmit(page, "needs_login", "Submitted login but still signed out (wrong password on this host? 2FA/OTP?) -- resolve manually.", "still-logged-out");
    return false;
  }
  return true;
}

/** A signed-in visitor who isn't a member sees no Join control on the home
 * feed of some communities -- only inside a space ("Join space" on
 * entrepreneur-bootcamp, live 2026-09-18). Try up to three spaces that aren't
 * events spaces (a button there could RSVP to an event instead) and stop on
 * the first that shows a Join control. False when none does. */
async function openSpaceWithJoin(page) {
  const paths = await evaluateWithRetry(page, () =>
    [...new Set([...document.querySelectorAll('a[href*="/c/"]')]
      .map((a) => { try { return new URL(a.href, location.href); } catch { return null; } })
      .filter((u) => u && u.origin === location.origin)
      .map((u) => u.pathname))]);
  const origin = new URL((await currentUrl(page)) || url).origin;
  for (const target of paths.filter((p) => !/event/i.test(p)).slice(0, 3)) {
    if (await navigate(page, origin + target)) continue;
    await waitForSettled(page);
    const st = await readPageState(page);
    if (st.hasJoin) return true;
  }
  return false;
}

async function joinFlow(page) {
  const nav = await navigate(page, url);
  if (nav) {
    if (nav.status === "dead_host") emit("dead_host", nav.detail, null);
    else await handOffAndEmit(page, nav.status, nav.detail);
    return;
  }
  await waitForSettled(page);

  let triedLogin = false;
  let triedSpace = false;
  // Extra rounds exist for (a) communities that show a signed-out visitor only
  // a Join button -- the click opens a sign-up/login dialog, we sign in, come
  // back and click Join again as a signed-in account -- and (b) a signed-in
  // non-member whose page has no Join control: we step into a space for it.
  for (let round = 0; round < 3; round++) {
    const state = await readPageState(page);
    if (await stoppedByGate(page, state)) return;

    const ms = await memberState(page);
    if (ms.otherAccount) {
      await handOffAndEmit(page, "wrong_account", `Another Circle account is signed in on ${state.url} -- not joining as it.`);
      return;
    }
    if (ms.isMember) {
      await finishMembership(page, round ? "Signed in; already a member" : "Already a member");
      return;
    }
    const signedOut = ms.api ? !ms.loggedIn : state.hasLogin;
    // Circle's API is the authority on "signed out"; without it (not a Circle
    // page) only a visible Log in control counts.
    if (signedOut && (state.hasLogin || ms.api) && !triedLogin) {
      triedLogin = true;
      if (!(await signIn(page, { hasLoginControl: state.hasLogin }))) return;
      continue; // re-read the page as a signed-in account
    }

    if (!state.hasJoin && ms.api && ms.loggedIn && !triedSpace) {
      triedSpace = true;
      if (await openSpaceWithJoin(page)) continue;
    }
    if (!state.hasJoin) {
      const who = !ms.api ? ", not a Circle community page" : ms.loggedIn ? ", signed in, not a member" : ", signed out";
      await handOffAndEmit(page, "unclear", `No Join control found (${state.url}${who}) -- needs a human look.`);
      return;
    }
    const clicked = await clickFirstMatch(page, JOIN_TEXTS);
    if (!clicked) {
      await handOffAndEmit(page, "unclear", "A Join-like control was detected but could not be clicked.", "join-control-not-clickable");
      return;
    }
    await page.waitForTimeout(1500);
    await waitForSettled(page);

    const after = await readPostClickState(page);
    // A checkout URL wins over everything else: signed in is not paying.
    if (after.onCheckout) {
      emit("paid_skip", `Clicked "${clicked}" but landed on checkout (${after.url}) -- no free tier reachable.`,
        await maybeScreenshot(page, "after-join"));
      return;
    }
    const ms2 = await memberState(page);
    if (ms2.otherAccount) {
      await handOffAndEmit(page, "wrong_account", "After the Join click a different Circle account is signed in -- stopped.");
      return;
    }
    if (ms2.isMember || after.newMemberOnboarding) {
      await finishMembership(page, `Clicked "${clicked}"`);
      return;
    }
    if (after.pending) {
      emit("pending_approval", `Clicked "${clicked}" -- community shows a pending-approval message.`,
        await maybeScreenshot(page, "after-join"));
      return;
    }
    if (ms2.api && !ms2.loggedIn && !triedLogin) {
      // The click opened a sign-up/login dialog for a signed-out visitor.
      triedLogin = true;
      if (!(await signIn(page))) return;
      const back = await navigate(page, url);
      if (back) {
        await handOffAndEmit(page, back.status === "dead_host" ? "nav_failed" : back.status, back.detail);
        return;
      }
      await waitForSettled(page);
      continue;
    }
    if (after.formQuestions.length > 0) {
      const signup = await tryCompleteSignupForm(page, clicked);
      if (signup.done) return;
      await handOffAndEmit(page, "application_form_detected",
        `Clicked "${clicked}"; a form appeared and was not filled (${signup.reason}). Questions: ` +
          JSON.stringify(after.formQuestions.slice(0, 10)));
      return;
    }
    await handOffAndEmit(page, "unclear",
      `Clicked "${clicked}" but Circle does not report a membership (${after.url}) -- needs a human look.`, "after-join");
    return;
  }
  await handOffAndEmit(page, "unclear", "Signed in, but the Join step did not produce a membership -- needs a human look.");
}

// takeOverTaskSpace, not taskSpace: the previous attempt in this same batch
// may have called task.handOff() and left the space user-owned. Re-invoking
// `auto-join --space-id N` is itself the operator's explicit ask to resume it.
const task = await takeOverTaskSpace(spaceId);
const page = task.page("p1");

try {
  await joinFlow(page);
} catch (err) {
  // A page-level surprise is about this community, not about the bridge:
  // report it so the batch moves on instead of stopping dead.
  if (!emitted) {
    await handOffAndEmit(page, "driver_error", `Driver error: ${String((err && err.message) || err).slice(0, 300)}`);
  }
}
if (!emitted) {
  emit("driver_error", "Driver finished without an outcome.", null);
}
