/* Bring-your-own OpenRouter key.
   One control shared by Compose and the Food Journal: a Key tab on each page.
   The key lives in this tab's sessionStorage, travels only on the two AI calls
   (see keyHeaders), and is gone when the tab closes. The server never reports
   or logs its value.

   Pages mount it once their /api/config arrives. A key failure switches to the
   Key tab with the reason, so the page never grows a second key dialog. */

const STORAGE_KEY = 'np_openrouter_key';
const HEADER = 'X-OpenRouter-Key';
/* shortcut: sessionStorage is per tab but survives a browser session restore,
   so a restored tab can keep the key. Use memory-only storage if that matters. */
const PLACEHOLDER_EMPTY = 'sk-or-v1-…';
const PLACEHOLDER_SAVED = 'Paste a new key to replace the saved one';

/** @type {string} */ let memoryKey = '';
/** @type {boolean} */ let keyRequired = false;
/** @type {boolean} */ let mounted = false;
/** @type {HTMLElement | null} */ let panel = null;
/** @type {HTMLInputElement | null} */ let input = null;
/** @type {HTMLButtonElement | null} */ let forget = null;
/** @type {HTMLButtonElement | null} */ let save = null;
/** @type {HTMLElement | null} */ let note = null;
/** @type {() => void} */ let reveal = () => {};
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
  if (!mounted) return;
  const saved = hasKey();
  if (input) input.placeholder = saved ? PLACEHOLDER_SAVED : PLACEHOLDER_EMPTY;
  if (forget) forget.hidden = !saved;
  if (save) save.textContent = saved ? 'Replace key' : 'Save key';
  if (problem) {
    setNote(problem);
    return;
  }
  setNote(
    saved ? 'Saved for this tab. It clears when you close the tab.' : '',
    saved,
  );
}

/** Switch to the Key tab and focus the field. */
function openKeySettings() {
  if (!mounted) return;
  paint();
  reveal();
  input?.focus();
}

/** Open the Key tab with the reason the last AI call was refused. */
export function showKeyProblem(message) {
  if (!mounted) return;
  problem = trimmed(message) || 'OpenRouter could not use the saved key.';
  openKeySettings();
}

/** Turn a 401 or 402 from either AI route into the shared key prompt. */
export function reportKeyFailure(status, message) {
  if (status === 401 || status === 402) showKeyProblem(message);
}

/**
 * Render the key form into a page's Key section.
 * A local-provider deployment gets a short note instead of a form.
 * @param {Element | null} slot
 * @param {{provider?: string, keyRequired?: boolean, open?: () => void}} [options]
 */
export function mountKeySettings(slot, options = {}) {
  if (!slot || mounted) return;
  mounted = true;
  keyRequired = options.keyRequired === true;
  reveal = options.open || (() => {});

  const local = options.provider === 'local';

  panel = document.createElement('section');
  panel.className = 'npkey-settings';
  panel.innerHTML = local
    ? `<p class="npkey-local"><strong>Local model.</strong> This deployment runs its
        own model, so Estimate and Cook need no key. Nothing here is sent to OpenRouter.</p>`
    : `<form class="npkey-form" novalidate>
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

  slot.replaceChildren(panel);
  if (local) return;

  input = /** @type {HTMLInputElement} */ (panel.querySelector('input'));
  note = /** @type {HTMLElement} */ (panel.querySelector('.npkey-note'));
  forget = /** @type {HTMLButtonElement} */ (panel.querySelector('.npkey-forget'));
  save = /** @type {HTMLButtonElement} */ (panel.querySelector('.npkey-save'));

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
    paint();
  });
  forget.addEventListener('click', () => {
    storeKey('');
    problem = '';
    paint();
  });
  paint();
}
