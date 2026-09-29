// The count beside "Требует внимания" in the menu: open items that are
// critical or need attention. Informational ones do not raise it.

import { $ } from './dom.js';

export function setBadge(data) {
  const badge = $('#attention-badge');
  if (!badge) return;
  const crit = data?.by_severity?.crit || 0;
  const warn = data?.by_severity?.warn || 0;
  const n = crit + warn;
  badge.hidden = n === 0;
  badge.textContent = String(n);
  badge.className = `badge ${crit ? 'badge-critical' : 'badge-warning'}`;
  badge.title = `критично: ${crit}, внимание: ${warn}`;
}
