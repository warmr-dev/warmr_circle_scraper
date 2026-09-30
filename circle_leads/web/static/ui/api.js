// One way to talk to the server: JSON in, JSON out, the viewer's time zone on
// every read (so "today" is the viewer's today), and a signed-out session sent
// back to the login page with the current view remembered.

const RETURN_KEY = 'warmr.returnHash';

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function remember(hash) {
  try { sessionStorage.setItem(RETURN_KEY, hash); } catch { /* private mode */ }
}

export function takeReturnHash() {
  try {
    const hash = sessionStorage.getItem(RETURN_KEY);
    sessionStorage.removeItem(RETURN_KEY);
    return hash;
  } catch {
    return null;
  }
}

export async function api(path, { method = 'GET', body, params } = {}) {
  const url = new URL(path, location.origin);
  if (method === 'GET') url.searchParams.set('tz', String(new Date().getTimezoneOffset()));
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, value);
  }
  const init = { method, headers: { Accept: 'application/json' }, credentials: 'same-origin' };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  if (res.status === 401) {
    remember(location.hash);
    location.href = '/login';
    throw new ApiError(401, 'Please sign in again');
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const detail = data && (data.detail || data.error);
    throw new ApiError(res.status, typeof detail === 'string' ? detail : `Error ${res.status}`);
  }
  return data;
}
