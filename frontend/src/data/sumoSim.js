// RemoteSim: stands in for engine/sim.js Simulation while the SUMO backend source is selected.
// It exposes what the renderer and controller read (net, cfg, signals, live, amb, snapshot(), logEntries) and fills them
// from the backend's WebSocket frames, so the renderer and its sprites are reused unchanged.
// Frames arrive as {veh: [{id, kind, edge, s, v, color, len, braking, prev}], signals, amb, log, t, queued}; edge and s are already
// in browser format (backend Step 9a). Frames are queued and played out on a steady clock (speed buttons change the clock; pause holds).
import { CONFIG } from '../engine/config.js';
import { buildNetwork, findRoute } from '../engine/network.js';
import { makeSignal } from '../engine/signals.js';
import { setPose } from '../engine/vehicles.js';
import { startRun, openStream, BackendError, scenarioAmbulances, setLiveTraffic, dispatchAmbulance } from './sumoApi.js';
import { ambColor, MAX_AMBS } from './ambColors.js';
import { normaliseMetrics } from './sumoCompare.js';

const BASE_PERIOD = 125; // ms between frames at 4x, the backend's own pace
const LOG_CAP = 400;

export class RemoteSim {
  constructor(scenario) {
    this.remote = true;
    this.ambient = false;
    this.cfg = CONFIG;
    this.scenario = { ...scenario, blocked: [...(scenario.blocked || [])] };
    this.strategyId = this.scenario.strategy || 'C';
    this.net = buildNetwork(CONFIG);
    this.signals = this.net.junctions.map((j) => {
      const s = makeSignal(j, CONFIG);
      j.sig = s;
      return s;
    });
    const path = findRoute(this.net, this.scenario.origin, this.scenario.dest, new Set(this.scenario.blocked));
    this.ambRoute = path || null;
    this.live = [];
    this.byId = new Map();
    this.amb = null;
    this.logEntries = [];
    this.logTotal = 0;
    this.series = [];
    this.t = 0;
    this.queued = 0;
    this.status = this.ambRoute ? 'ready' : 'no-route';
    this.finished = false;
    this.error = null;
    this.metrics = null;
    this.info = null;
    this.queue = [];
    this.stepAt = 0;
    this.period = BASE_PERIOD;
    this.summary = null;
    this.ambSeen = false;
    this.ambDone = false;
    this.closeStream = null;
    // Step 10b: several ambulances and live changes (SUMO only)
    this.ambPlan = scenarioAmbulances(this.scenario);        // null = the ordinary single ambulance
    this.ambCountTotal = this.ambPlan ? this.ambPlan.length : 1;
    this.ambs = new Map();                                    // id -> vehicle object, kept after the ambulance has left
    this.ambTints = null;                                     // id -> colour, only when there are 2 or more ambulances
    this.runId = null;
    this.modifiedLive = false;
    this.liveError = null;
    this.stale = true;
    this._dirty = true;
  }

  // ----- lifecycle
  async start() {
    try {
      const r = await startRun(this.scenario, this.strategyId, { live: true, ambulances: this.ambPlan });
      this.info = r;
      this.runId = r.id;
      this._dirty = true;
      if (this.closed) return;
      this.closeStream = openStream(r.id, {
        message: (m) => this.onMessage(m),
        error: (e) => this.fail(e),
        close: () => {
          if (!this.summary && !this.error) this.fail(new BackendError('The SUMO backend closed the stream before the run finished.', 'http'));
        },
      });
    } catch (e) {
      this.fail(e);
    }
  }
  dispose() {
    this.closed = true;
    if (this.closeStream) this.closeStream();
  }
  fail(e) {
    if (this.closed) return;
    this.error = e instanceof BackendError ? e : new BackendError(String((e && e.message) || e), 'http');
    this._dirty = true;           // finished stays false: the run did not complete, so no summary card is shown
  }
  onMessage(m) {
    if (this.closed) return;
    if (m.type === 'snapshot') this.queue.push(m);
    else if (m.type === 'summary') this.summary = m;
    else if (m.type === 'error') this.fail(new BackendError(`SUMO run failed: ${m.detail || 'unknown error'}`, 'run'));
  }

  // ----- playback (called from the controller's frame loop; no React involved)
  advance(now, playing, speed) {
    this.period = (BASE_PERIOD * 4) / Math.max(0.25, speed || 4);
    if (playing && this.queue.length && now - this.stepAt >= this.period) {
      this.apply(this.queue.shift());
      this.stepAt = this.stepAt && now - this.stepAt < 3 * this.period ? this.stepAt + this.period : now;
    }
    if (!this.finished && this.summary && !this.queue.length) this.complete();
  }
  alphaAt(now) {
    return this.queue.length || now - this.stepAt < this.period ? Math.min(1, Math.max(0, (now - this.stepAt) / this.period)) : 1;
  }
  consumeDirty() {
    const d = this._dirty;
    this._dirty = false;
    return d;
  }

  apply(m) {
    this.t = m.t;
    this.queued = m.queued;
    this.status = 'running';
    for (const sg of this.signals) {
      const s = m.signals && m.signals[sg.id];
      if (s) {
        sg.greenAxis = s.green_axis;
        sg.phase = s.phase;
        sg.timer = s.timer;
      }
    }
    const ed = this.net.edgeById;
    const seen = new Set();
    for (const v of m.veh || []) {
      const edge = ed[v.edge];
      if (!edge) continue;
      seen.add(v.id);
      let o = this.byId.get(v.id);
      const fresh = !o;
      if (fresh) {
        o = { id: v.id, kind: v.kind, color: v.color, len: v.len, edge: null, prevEdge: null, route: [], ri: 0, s: 0, v: 0, braking: false,
          x: 0, y: 0, h: 0, px: 0, py: 0, ph: 0, rx: 0, ry: 0, rh: 0, inNet: true, done: false, waitTotal: 0, stops: 0 };
        this.byId.set(v.id, o);
        this.live.push(o);
      } else {
        o.px = o.x;
        o.py = o.y;
        o.ph = o.h;
      }
      if (v.prev && ed[v.prev]) o.prevEdge = ed[v.prev];
      else if (o.edge && o.edge !== edge) o.prevEdge = o.edge;
      o.edge = edge;
      o.s = v.s;
      o.v = v.v;
      o.braking = v.braking;
      setPose(o, CONFIG);
      if (fresh) {
        o.px = o.x;
        o.py = o.y;
        o.ph = o.h;
      }
      if (o.kind === 'amb') {
        this.ambs.set(o.id, o);
        this.ambSeen = true;
      }
    }
    if (seen.size !== this.live.length) {
      let w = 0;
      for (const o of this.live) {
        if (seen.has(o.id)) this.live[w++] = o;
        else {
          this.byId.delete(o.id);
          if (o.kind === 'amb') {
            o.inNet = false;
            o.done = true;
          }
        }
      }
      this.live.length = w;
    }
    this.updateAmbs();
    for (const e of m.log || []) {
      this.logEntries.push({ id: ++this.logTotal, t: e.t, kind: e.kind, text: e.text, amb: this.logAmb(e) });
      if (this.logEntries.length > LOG_CAP) this.logEntries.shift();
    }
    this.series.push([m.t, m.queued]);
    if (this.series.length > 400) this.series.shift();
  }

  // The ambulance the camera and the single-ambulance panels follow: ambulance 1 while it drives, then the next one on the road.
  // With one ambulance this is exactly the old behaviour.
  updateAmbs() {
    const list = [...this.ambs.values()];
    const active = list.filter((a) => a.inNet && !a.done);
    this.amb = list.find((a) => a.id === 'amb0' && active.includes(a)) || active[0] || list.find((a) => a.id === 'amb0') || list[list.length - 1] || null;
    this.ambDone = list.length > 0 && list.length >= this.ambCountTotal && list.every((a) => a.done);
    if (this.ambCountTotal > 1) {
      const t = {};
      for (const a of list) t[a.id] = ambColor(a.id);
      this.ambTints = t;
    }
  }

  // Which ambulance a log line belongs to: the backend tags most lines; amb0's own enter/arrive/pass lines are untagged.
  logAmb(e) {
    const d = e.data && e.data.amb;
    if (d) return d;
    const m = /^\[(amb\d)\]/.exec(e.text || '');
    if (m) return m[1];
    if (this.ambCountTotal > 1 && (e.kind === 'enter' || e.kind === 'arrive' || e.kind === 'pass') && !/amb\d/.test(e.text || '')) return 'amb0';
    return null;
  }

  // ----- live changes (the run must be going; the backend applies them at its current simulated time)
  get canLive() {
    return !!(this.runId && !this.finished && !this.error && !this.summary);
  }
  get canDispatch() {
    return this.canLive && this.ambCountTotal < MAX_AMBS;
  }
  async dispatch(origin, dest) {
    if (!this.canDispatch) return false;
    try {
      const r = await dispatchAmbulance(this.runId, origin, dest);
      this.ambCountTotal = Math.max(this.ambCountTotal, (r.ambulance_count || this.ambCountTotal + 1));
      this.modifiedLive = true;
      this.liveError = null;
    } catch (e) {
      this.liveError = e instanceof BackendError ? e.message : String((e && e.message) || e);
    }
    this._dirty = true;
    return true;
  }

  complete() {
    const s = this.summary;
    this.metrics = normaliseMetrics(s.summary.metrics);
    this.info = { ...(this.info || {}), ...s };
    this.status = 'done';
    this.finished = true;
    this._dirty = true;
  }

  // Live traffic change. Returns false when nothing was changed (same setting, or the run is not live), so the
  // controller does not mark the run as modified for nothing.
  setDensity(d, rate) {
    const cur = this.scenario;
    if (!this.canLive || (d === cur.density && (d !== 'custom' || Math.round(Number(rate)) === Math.round(Number(cur.rate))))) return false;
    this.scenario = { ...cur, density: d, ...(rate !== undefined ? { rate } : {}) };
    this.modifiedLive = true;
    setLiveTraffic(this.runId, d === 'custom' ? { rate: Math.round(Number(rate)) } : { density: d })
      .then(() => {
        this.liveError = null;
        this._dirty = true;
      })
      .catch((e) => {
        this.liveError = e instanceof BackendError ? e.message : String((e && e.message) || e);
        this._dirty = true;
      });
    return true;
  }

  snapshot() {
    const m = this.metrics;
    const a = this.amb;
    const info = this.info || {};
    return {
      t: this.t,
      status: this.error ? 'error' : this.status,
      finished: this.finished,
      strategy: this.strategyId,
      scenario: this.scenario,
      metrics: m || {
        ambTravelTime: null, ambFinished: false, ambStops: 0, avgWait: null, longestWait: null, maxQueue: null, throughput: null,
        completed: null, entered: null, recoveryTime: null, recoveryBaseline: null, violations: 0, totalQueued: this.queued, time: this.t,
      },
      ambEta: null,
      ambActive: !!(a && a.inNet && !a.done),
      ambCleared: this.ambDone,
      ambDone: this.ambDone,
      ambNext: null,
      logTotal: this.logTotal,
      signals: this.signals.map((s) => ({ id: s.id, greenAxis: s.greenAxis, phase: s.phase, timer: s.timer })),
      vehicleCount: this.live.length,
      series: this.series.slice(-140),
      hasRoute: !!this.ambRoute,
      sumo: {
        waiting: !this.error && !this.queue.length && !this.finished && this.status !== 'running',
        scale: info.calibration_scale ?? null,
        rate: info.rate_per_entry_veh_h ?? null,
        oversaturated: info.oversaturated ?? null,
        trafficNote: info.traffic_note || null,
      },
      backendError: this.error ? this.error.message : null,
      // Step 10b (added fields): present in every SUMO snapshot, used only by the SUMO controls
      ambs: [...this.ambs.values()].map((a) => ({ id: a.id, color: ambColor(a.id), active: a.inNet && !a.done, done: a.done })),
      ambCount: this.ambCountTotal,
      canDispatch: this.canDispatch,
      canLive: this.canLive,
      modifiedLive: this.modifiedLive,
      liveError: this.liveError,
    };
  }
}
