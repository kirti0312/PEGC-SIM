// Which engine produces the numbers: the browser mini-simulator (default) or the SUMO backend.
// One API-address setting. Plain module (no React) so the controller, stores and tests can use it.
export const DEFAULT_API = (typeof import.meta !== 'undefined' && import.meta.env && import.meta.env.VITE_API_BASE) || 'http://localhost:8000';
const K_SRC = 'pegc.source';
const K_API = 'pegc.apiBase';

function load(key) {
  try {
    return typeof localStorage !== 'undefined' ? localStorage.getItem(key) : null;
  } catch {
    return null;
  }
}
function save(key, v) {
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(key, v);
  } catch {
    /* storage can be blocked; the setting then lasts for this page only */
  }
}

export const cleanBase = (u) => String(u || '').trim().replace(/\/+$/, '') || DEFAULT_API;

let state = { kind: load(K_SRC) === 'sumo' ? 'sumo' : 'browser', apiBase: cleanBase(load(K_API)) };
const subs = new Set();
const set = (p) => {
  state = { ...state, ...p };
  subs.forEach((f) => f());
};

export const subscribeSource = (f) => (subs.add(f), () => subs.delete(f));
export const getSource = () => state;
export const isSumo = () => state.kind === 'sumo';
export const getApiBase = () => state.apiBase;
export function setSource(kind) {
  const k = kind === 'sumo' ? 'sumo' : 'browser';
  save(K_SRC, k);
  set({ kind: k });
}
export function setApiBase(url) {
  const u = cleanBase(url);
  save(K_API, u);
  set({ apiBase: u });
}

export const BROWSER_LABEL = 'Browser mini-simulator: not SUMO results';
export const SUMO_LABEL = 'SUMO backend: real SUMO traffic simulation';
