// HTTP and WebSocket access to the SUMO backend (Step 8 API). Every failure becomes a BackendError with a plain message.
import { getApiBase } from './source.js';

export class BackendError extends Error {
  constructor(message, kind = 'http') {
    super(message);
    this.kind = kind; // 'unreachable' | 'http' | 'run'
  }
}

export const unreachableMessage = (base = getApiBase()) =>
  `The SUMO backend is not reachable at ${base}. Start it (cd backend && uvicorn api.main:app --port 8000) and check the address, or switch to the Browser mini-simulator.`;

async function req(path, opts) {
  const base = getApiBase();
  let res;
  try {
    res = await fetch(base + path, opts);
  } catch {
    throw new BackendError(unreachableMessage(base), 'unreachable');
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* no JSON body */
  }
  if (!res.ok) {
    const d = body && body.detail;
    const msg = typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => x.msg || JSON.stringify(x)).join('; ') : `HTTP ${res.status}`;
    throw new BackendError(`SUMO backend: ${msg}`, 'http');
  }
  return body;
}

export const health = () => req('/api/health');

// Browser scenario -> API body. Presets go as names (the backend applies the calibrated rates); only custom sends a rate.
export function runBody(scenario, strategy) {
  const b = {
    strategy,
    origin: scenario.origin,
    dest: scenario.dest,
    density: scenario.density,
    seed: Math.round(Number(scenario.seed)),
    blocked: [...(scenario.blocked || [])],
  };
  if (scenario.density === 'custom') b.rate = Math.round(Number(scenario.rate));
  return b;
}

export const startRun = (scenario, strategy) =>
  req('/api/runs', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(runBody(scenario, strategy)) });
export const getRun = (id, withLog = false) => req(`/api/runs/${encodeURIComponent(id)}${withLog ? '?log=1' : ''}`);

export function wsUrl(id) {
  return getApiBase().replace(/^http/i, 'ws') + `/ws/runs/${encodeURIComponent(id)}`;
}

// Opens the live stream of one run. handlers: { message(obj), error(BackendError), close() }. Returns a function that closes it.
export function openStream(id, handlers) {
  let ws;
  let closedByUs = false;
  try {
    ws = new WebSocket(wsUrl(id));
  } catch {
    handlers.error(new BackendError(unreachableMessage(), 'unreachable'));
    return () => {};
  }
  let opened = false;
  ws.onopen = () => {
    opened = true;
  };
  ws.onmessage = (ev) => {
    try {
      handlers.message(JSON.parse(ev.data));
    } catch {
      /* ignore a malformed frame */
    }
  };
  ws.onerror = () => {
    if (!closedByUs) handlers.error(new BackendError(opened ? 'Lost the connection to the SUMO backend.' : unreachableMessage(), opened ? 'http' : 'unreachable'));
  };
  ws.onclose = () => {
    if (!closedByUs) handlers.close();
  };
  return () => {
    closedByUs = true;
    try {
      ws.close();
    } catch {
      /* already closed */
    }
  };
}
