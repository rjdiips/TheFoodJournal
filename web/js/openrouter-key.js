/* Bring-your-own OpenRouter key.
   One control shared by Compose and the Food Journal: a quiet pill in each
   page's header that opens a drawer. The key lives in this tab's
   sessionStorage, travels only on the two AI calls (see keyHeaders), and is
   gone when the tab closes. The server never reports or logs its value.

   Pages mount it once their /api/config arrives, because a local provider has
   no use for a key. A key failure opens the same drawer with the reason
   instead of a second dialog. */

const STORAGE_KEY = 'np_openrouter_key';
const HEADER = 'X-OpenRouter-Key';
/* shortcut: sessionStorage is per tab but survives a browser session restore,
   so a restored tab can keep the key. Use memory-only storage if that matters. */
const PLACEHOLDER_EMPTY = 'sk-or-v1-…';
const PLACEHOLDER_SAVED = 'Paste a new key to replace the saved one';

/** @type {string} */ let memoryKey = '';
/** @type {boolean} */ let keyRequired = false;
/** @type {HTMLButtonElement | null} */ let trigger = null;
/** @type {HTMLElement | null} */ let panel = null;
/** @type {HTMLElement | null} */ let scrim = null;
/** @type {HTMLInputElement | null} */ let input = null;
/** @type {HTMLButtonElement | null} */ let forget = null;
/** @type {HTMLElement | null} */ let note = null;
/** @type {string} */ let problem = '';

const trimmed = (value) => String(value || '').trim();

/** The key this tab holds, if any. @returns {string} */
export function getKey() {
  try {
    return trimmed(sessionStorage.getItem(STORAGE_KEY)) || memoryKey;
  } catch {
    /* Storage can be switched off; the in-memory copy still works this tab. */
    return memoryKey;
  }
}

export function hasKey() {
  return Boolean(getKey());
}

/** True when the deployment has no key and this tab has not supplied one. */
export function needsKey() {
  return keyRequired && !hasKey();
}

/** Headers to merge into /api/parse and /api/leftover-alchemist only. */
export function keyHeaders() {
  const key = getKey();
  return key ? { [HEADER]: key } : {};
}

/** @param {string} value */
function storeKey(value) {
  memoryKey = value;
  try {
    if (value) sessionStorage.setItem(STORAGE_KEY, value);
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // The memory copy above lasts for this tab, which is all the key promises.
  }
}

function setNote(text, ok = false) {
  if (!note) return;
  note.textContent = text;
  note.className = 'npkey-note' + (text ? (ok ? ' is-ok' : ' is-bad') : '');
  note.hidden = !text;
}

function paint() {
  if (!trigger) return;
  const saved = hasKey();
  trigger.classList.toggle('is-set', saved && !problem);
  trigger.classList.toggle('is-bad', Boolean(problem));
  const label = trigger.querySelector('.npkey-label');
  if (label) {
    label.textContent = problem
      ? 'Key problem'
      : saved
        ? 'Key saved'
        : keyRequired
          ? 'Add your key'
          : 'OpenRouter key';
  }
  if (input) {
    input.placeholder = saved ? PLACEHOLDER_SAVED : PLACEHOLDER_EMPTY;
  }
  if (forget) forget.hidden = !saved;
  if (panel) {
    const title = panel.querySelector('.npkey-title');
    const save = panel.querySelector('.npkey-save');
    if (title) title.textContent = saved ? 'Your OpenRouter key' : 'Add an OpenRouter key';
    if (save) save.textContent = saved ? 'Replace key' : 'Save key';
  }
}

function close() {
  if (!panel || !scrim) return;
  panel.hidden = true;
  scrim.hidden = true;
  if (input) input.value = '';
  if (trigger) trigger.focus();
}

/** Open the drawer without claiming a problem. */
export function openKeyPanel() {
  if (!panel || !scrim || !input) return;
  setNote(problem, false);
  paint();
  if (!panel.hidden) {
    input.focus();
    return;
  }
  scrim.hidden = false;
  panel.hidden = false;
  input.focus();
}

/** Open the drawer with the reason the last AI call was refused. */
export function showKeyProblem(message) {
  if (!panel) return;
  problem = trimmed(message) || 'OpenRouter could not use the saved key.';
  openKeyPanel();
}

/** Turn a 401 or 402 from either AI route into the shared key prompt. */
export function reportKeyFailure(status, message) {
  if (status === 401 || status === 402) showKeyProblem(message);
}

function buildPanel() {
  scrim = document.createElement('div');
  scrim.className = 'npkey-scrim';
  scrim.hidden = true;

  panel = document.createElement('aside');
  panel.className = 'npkey-drawer';
  panel.hidden = true;
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-labelledby', 'npKeyTitle');
  panel.innerHTML = `
    <div class="npkey-head">
      <div>
        <span class="npkey-kicker">Your key, your tab</span>
        <h2 class="npkey-title" id="npKeyTitle">Add an OpenRouter key</h2>
      </div>
      <button type="button" class="npkey-close" aria-label="Close">&times;</button>
    </div>
    <p class="npkey-copy">Estimate and Cook call OpenRouter with this key. It stays in
      this browser tab, is sent only with those two calls, and is gone when the tab
      closes. No account, no login.</p>
    <form class="npkey-form" novalidate>
      <label class="npkey-field"><span>OpenRouter key</span>
        <input type="password" spellcheck="false" autocomplete="off"
          placeholder="${PLACEHOLDER_EMPTY}" aria-describedby="npKeyNote" />
      </label>
      <p class="npkey-hint">
        <a href="https://openrouter.ai/keys" target="_blank" rel="noopener">Create or manage keys on OpenRouter</a>.</p>
      <p class="npkey-note" id="npKeyNote" role="status" hidden></p>
      <div class="npkey-foot">
        <button type="submit" class="npkey-save">Save key</button>
        <button type="button" class="npkey-forget" hidden>Remove key</button>
      </div>
    </form>`;

  input = /** @type {HTMLInputElement} */ (panel.querySelector('input'));
  note = /** @type {HTMLElement} */ (panel.querySelector('.npkey-note'));
  forget = /** @type {HTMLButtonElement} */ (panel.querySelector('.npkey-forget'));

  panel.querySelector('.npkey-close')?.addEventListener('click', close);
  scrim.addEventListener('click', close);
  panel.querySelector('form')?.addEventListener('submit', (event) => {
    event.preventDefault();
    const value = trimmed(input?.value);
    if (!value) {
      setNote('Paste your OpenRouter key first.');
      input?.focus();
      return;
    }
    storeKey(value);
    problem = '';
    if (input) input.value = '';
    setNote('Saved for this tab. It clears when you close the tab.', true);
    paint();
  });
  forget.addEventListener('click', () => {
    storeKey('');
    problem = '';
    setNote('Removed from this tab.');
    paint();
  });

  document.addEventListener('keydown', (event) => {
    if (!panel || panel.hidden) return;
    if (event.key === 'Escape') {
      close();
      return;
    }
    if (event.key !== 'Tab') return;
    const items = /** @type {HTMLElement[]} */ (
      [...panel.querySelectorAll('button, input, a[href]')]
        .filter((node) => !(/** @type {HTMLElement} */ (node)).hidden)
    );
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  document.body.append(scrim, panel);
}

/**
 * Put the key pill in `slot` and the drawer on the page.
 * @param {Element | null} slot
 * @param {{available?: boolean, keyRequired?: boolean}} [options]
 */
export function mountKeyControl(slot, options = {}) {
  if (!slot || options.available === false || panel) return;
  keyRequired = options.keyRequired === true;
  buildPanel();

  trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'npkey-trigger';
  trigger.setAttribute('aria-haspopup', 'dialog');
  trigger.innerHTML = '<span class="npkey-dot" aria-hidden="true"></span>'
    + '<span class="npkey-label">OpenRouter key</span><small>this tab</small>';
  trigger.addEventListener('click', openKeyPanel);
  slot.append(trigger);
  paint();
}
