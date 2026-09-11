// Reads this tab's Circle session cookies and POSTs them to the Warmr
// dashboard's existing /api/connections/{host}/session endpoint -- the same
// call the dashboard's "Join Queue" tab makes when you paste a cookie by
// hand. This just replaces the copy/paste with one click, reading the two
// cookies straight from the browser (they are almost certainly HttpOnly, so
// this needs the privileged chrome.cookies API, not page JavaScript).

const DASHBOARD_URL_DEFAULT = 'https://warmr-circle-scraper.vercel.app';
// Matches SESSION_COOKIE_NAMES in circle_leads/scraper/member_api_reader.py.
const COOKIE_NAMES = ['_circle_session', 'user_session_identifier', 'remember_user_token'];

const statusEl = document.getElementById('status');
const hostEl = document.getElementById('host');
const sendBtn = document.getElementById('send');

function setStatus(msg, kind) {
  statusEl.textContent = msg;
  statusEl.className = kind || '';
}

async function getSettings() {
  const { dashboardUrl, extensionToken } =
    await chrome.storage.local.get(['dashboardUrl', 'extensionToken']);
  return {
    dashboardUrl: (dashboardUrl || DASHBOARD_URL_DEFAULT).replace(/\/+$/, ''),
    extensionToken: extensionToken || '',
  };
}

async function currentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

let currentHost = null;

(async function init() {
  const tab = await currentTab();
  if (!tab || !tab.url || !tab.url.startsWith('http')) {
    hostEl.textContent = '(no page)';
    sendBtn.disabled = true;
    setStatus('Open a Circle community tab first.', 'err');
    return;
  }
  currentHost = new URL(tab.url).host;
  hostEl.textContent = currentHost;

  const { extensionToken } = await getSettings();
  if (!extensionToken) {
    setStatus('Set the extension token in Settings first.', 'err');
    sendBtn.disabled = true;
  }
})();

sendBtn.addEventListener('click', async () => {
  sendBtn.disabled = true;
  setStatus('Reading cookies…');
  try {
    const tab = await currentTab();
    const cookies = await chrome.cookies.getAll({ url: tab.url });
    const found = cookies.filter((c) => COOKIE_NAMES.includes(c.name));
    if (!found.length) {
      setStatus(`No Circle session cookies found on ${currentHost}. Are you logged in here?`, 'err');
      return;
    }

    const { dashboardUrl, extensionToken } = await getSettings();
    if (!extensionToken) {
      setStatus('Set the extension token in Settings first.', 'err');
      return;
    }

    setStatus(`Sending ${found.length} cookie(s)…`);
    const resp = await fetch(
      `${dashboardUrl}/api/connections/${encodeURIComponent(currentHost)}/session`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Extension-Token': extensionToken },
        body: JSON.stringify({ cookies: found.map((c) => ({ name: c.name, value: c.value })) }),
      },
    );
    if (!resp.ok) {
      const text = await resp.text();
      setStatus(`Failed (${resp.status}): ${text.slice(0, 160)}`, 'err');
      return;
    }
    const data = await resp.json();
    if (data.missing && data.missing.length) {
      setStatus(`Saved, but missing: ${data.missing.join(', ')} — a scan may fail.`, 'warn');
    } else {
      setStatus(`Saved ${data.cookies} cookie(s) for ${currentHost}. The worker will pick it up.`, 'ok');
    }
  } catch (e) {
    setStatus(`Error: ${String((e && e.message) || e).slice(0, 160)}`, 'err');
  } finally {
    sendBtn.disabled = false;
  }
});

document.getElementById('open-options').addEventListener('click', (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});
