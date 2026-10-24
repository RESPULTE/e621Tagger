// Stash loads this after login. Keep the extension independent of private React internals.
(() => {
  const addLink = () => {
    if (document.querySelector('[data-tag-folders-link]')) return;
    const nav = document.querySelector('.navbar-nav');
    if (!nav) return;
    const link = document.createElement('a');
    link.href = '/plugin/home-media/assets/index.html';
    link.textContent = 'Tag folders';
    link.className = 'nav-link';
    link.dataset.tagFoldersLink = 'true';
    nav.append(link);
  };
  addLink();
  const observer = new MutationObserver(addLink);
  observer.observe(document.body, { childList: true, subtree: true });
})();
