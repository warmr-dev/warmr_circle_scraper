// The page's address is its state: #/<section>?<filters>. A reload, the back
// button and a link pasted into a chat all land on the same view.

const listeners = [];

export function parse() {
  const hash = location.hash.replace(/^#\/?/, '');
  const [name, query] = hash.split('?');
  return { name: name || '', params: new URLSearchParams(query || '') };
}

function build(name, params) {
  const clean = Object.entries(params || {})
    .filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== false);
  const query = new URLSearchParams(clean).toString();
  return `#/${name}${query ? `?${query}` : ''}`;
}

export function navigate(name, params = {}, { replace = false } = {}) {
  const hash = build(name, params);
  if (hash === location.hash) {
    emit();
  } else if (replace) {
    history.replaceState(null, '', hash);
    emit();
  } else {
    location.hash = hash;  // hashchange -> emit
  }
}

// Change the address without re-rendering (closing a drawer, say).
export function rewrite(name, params = {}) {
  history.replaceState(null, '', build(name, params));
}

export function toObject(params) {
  return Object.fromEntries(params.entries());
}

export function onRoute(fn) {
  listeners.push(fn);
}

export function emit() {
  const route = parse();
  for (const fn of listeners) fn(route);
}

window.addEventListener('hashchange', emit);

// Where a link from one section (an attention item, a tile) points.
export function follow(link) {
  if (!link) return;
  if (link.section === 'communities' && link.id) {
    navigate('communities', { open: `c:${link.id}` });
  } else if (link.section === 'log') {
    navigate('attention', { tab: 'log', q: link.q });
  } else {
    const { section, ...rest } = link;
    navigate(section, rest);
  }
}
