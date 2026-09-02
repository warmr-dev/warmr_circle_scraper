/*
 * Circle feed grabber — paste into the browser console on a Circle space feed
 * you are a member of. It reads the posts your browser has already rendered
 * and copies them to your clipboard as plain text, ready to paste into
 * `circle-leads triage`.
 *
 * This is you reading your own feed: it touches only what is already on the
 * page in front of you. It sends nothing anywhere. Scroll to load more posts
 * first, then run it.
 */
(function () {
  const SELECTORS = [
    '[data-testid="post"]',
    'article',
    '.post-body, .post__body, .feed-item, [class*="PostCard"], [class*="post-card"]',
  ];
  const seen = new Set();
  const posts = [];

  for (const sel of SELECTORS) {
    document.querySelectorAll(sel).forEach((el) => {
      const text = (el.innerText || '').trim();
      if (text.length < 20) return;
      const key = text.slice(0, 80);
      if (seen.has(key)) return;
      seen.add(key);
      posts.push(text.replace(/\n{3,}/g, '\n\n'));
    });
    if (posts.length) break; // first selector that matches wins
  }

  if (!posts.length) {
    console.warn('[circle-leads] No posts found. Scroll the feed to load posts, then re-run.');
    alert('No posts found on this page. Scroll to load the feed, then run again.');
    return;
  }

  const blob = posts.join('\n\n---\n\n');
  const done = () => {
    console.log(`[circle-leads] Copied ${posts.length} post(s) to your clipboard.`);
    alert(`Copied ${posts.length} post(s). Paste them into circle-leads triage.`);
  };

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(blob).then(done, () => {
      console.log('[circle-leads] Clipboard blocked; here is the text:\n\n' + blob);
      alert('Clipboard was blocked. Open the console (⌥⌘J) and copy the printed text.');
    });
  } else {
    console.log('[circle-leads] Copy the text below:\n\n' + blob);
  }
})();
