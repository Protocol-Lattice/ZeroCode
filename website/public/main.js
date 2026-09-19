document.body.classList.add('js');

const menuToggle = document.querySelector('.menu-toggle');
const navigation = document.querySelector('#navigation');

function closeMenu() {
  menuToggle.setAttribute('aria-expanded', 'false');
  menuToggle.setAttribute('aria-label', 'Open navigation');
  navigation.classList.remove('is-open');
}

menuToggle.addEventListener('click', () => {
  const isOpen = menuToggle.getAttribute('aria-expanded') === 'true';
  menuToggle.setAttribute('aria-expanded', String(!isOpen));
  menuToggle.setAttribute('aria-label', isOpen ? 'Open navigation' : 'Close navigation');
  navigation.classList.toggle('is-open', !isOpen);
});
navigation.addEventListener('click', (event) => {
  if (event.target.closest('a')) closeMenu();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && menuToggle.getAttribute('aria-expanded') === 'true') {
    closeMenu();
    menuToggle.focus();
  }
});
document.addEventListener('click', (event) => {
  if (!event.target.closest('.site-header')) closeMenu();
});
window.matchMedia('(min-width: 801px)').addEventListener('change', (event) => {
  if (event.matches) closeMenu();
});

// Platform tabs support arrows, Home/End, and a single Tab stop.
function connectTabs(selector, onSelect) {
  const tabs = [...document.querySelectorAll(selector)];
  function select(tab, moveFocus = false) {
    for (const item of tabs) {
      const selected = item === tab;
      item.setAttribute('aria-selected', String(selected));
      item.tabIndex = selected ? 0 : -1;
    }
    onSelect(tab);
    if (moveFocus) tab.focus();
  }
  for (const tab of tabs) {
    tab.addEventListener('click', () => select(tab));
    tab.addEventListener('keydown', (event) => {
      const index = tabs.indexOf(tab);
      const next = {
        ArrowRight: (index + 1) % tabs.length,
        ArrowLeft: (index + tabs.length - 1) % tabs.length,
        Home: 0,
        End: tabs.length - 1,
      }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      select(tabs[next], true);
    });
  }
}

const requirements = {
  macos: 'Requires Node.js 24+, Xcode Command Line Tools, libcurl headers, curl, tar, and shasum. The first build downloads the pinned Zero compiler.',
  linux: 'Requires Node.js 24+, a C compiler, make, Git, curl, tar, libcurl development headers, and sha256sum. On Debian/Ubuntu, install build-essential and libcurl4-openssl-dev. The first build downloads the pinned Zero compiler.',
};
connectTabs('[data-os]', (tab) => {
  document.querySelector('#requirements-text').textContent = requirements[tab.dataset.os];
  document.querySelector('#install-panel').setAttribute('aria-labelledby', tab.id);
});

const toast = document.querySelector('.toast');
let toastTimer;
const buttonTimers = new WeakMap();

function announce(message) {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.classList.add('is-visible');
  toastTimer = setTimeout(() => toast.classList.remove('is-visible'), 4000);
}

for (const button of document.querySelectorAll('[data-copy]')) {
  const originalLabel = button.getAttribute('aria-label');
  const buttonText = button.querySelector('span');
  button.addEventListener('click', async () => {
    const source = document.getElementById(button.dataset.copy);
    try {
      await navigator.clipboard.writeText(source.textContent.trim());
      clearTimeout(buttonTimers.get(button));
      button.classList.add('is-copied');
      button.setAttribute('aria-label', 'Copied to clipboard');
      if (buttonText) buttonText.textContent = 'Copied';
      announce('Command copied to clipboard.');
      buttonTimers.set(button, setTimeout(() => {
        button.classList.remove('is-copied');
        button.setAttribute('aria-label', originalLabel);
        if (buttonText) buttonText.textContent = 'Copy';
      }, 2500));
    } catch {
      const range = document.createRange();
      range.selectNodeContents(source);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      announce('Command selected. Press ⌘C or Ctrl+C to copy.');
    }
  });
}
