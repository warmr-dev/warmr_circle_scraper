const DASHBOARD_URL_DEFAULT = 'https://warmr-circle-scraper.vercel.app';

(async function init() {
  const { dashboardUrl, extensionToken } =
    await chrome.storage.local.get(['dashboardUrl', 'extensionToken']);
  document.getElementById('dashboardUrl').value = dashboardUrl || DASHBOARD_URL_DEFAULT;
  document.getElementById('extensionToken').value = extensionToken || '';
})();

document.getElementById('save').addEventListener('click', async () => {
  const dashboardUrl = document.getElementById('dashboardUrl').value.trim() || DASHBOARD_URL_DEFAULT;
  const extensionToken = document.getElementById('extensionToken').value.trim();
  await chrome.storage.local.set({ dashboardUrl, extensionToken });
  const saved = document.getElementById('saved');
  saved.textContent = 'Saved.';
  setTimeout(() => { saved.textContent = ''; }, 2000);
});
