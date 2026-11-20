// Node tests for the SUMO data source (no browser, no backend: fetch is stubbed). Run: npm run test:data
import { RemoteSim } from '../sumoSim.js';
import { runBody, startRun, BackendError } from '../sumoApi.js';
import { runSumoComparison, normaliseMetrics } from '../sumoCompare.js';
import { getSource, setSource, setApiBase, isSumo, DEFAULT_API } from '../source.js';
import { defaultScenario } from '../../engine/config.js';

let pass = 0;
let fail = 0;
const t = async (name, fn) => {
  try {
    await fn();
    pass++;
    console.log('PASS ', name);
  } catch (e) {
    fail++;
    console.log('FAIL ', name, '\n     ', e.message);
  }
};
const ok = (c, m) => {
  if (!c) throw new Error(m || 'assertion failed');
};

const sc = { ...defaultScenario(), origin: 'W0', dest: 'E1', density: 'medium', seed: 7, strategy: 'C', blocked: [] };
const frame = (tm, veh, extra = {}) => ({
  type: 'snapshot', t: tm, queued: 3, vehicles: veh.length,
  signals: { J00: { phase: 'Y', green_axis: 'V', timer: 1.5 } }, amb: null, log: [], veh, ...extra,
});
const car = (id, edge, s, extra = {}) => ({ id, kind: 'car', edge, s, v: 5, color: 2, len: 4.5, braking: false, prev: null, ...extra });

await t('source defaults to the browser engine and one API address', () => {
  ok(getSource().kind === 'browser' && !isSumo());
  ok(getSource().apiBase === DEFAULT_API && DEFAULT_API === 'http://localhost:8000');
  setApiBase(' http://example.test:9000/// ');
  ok(getSource().apiBase === 'http://example.test:9000');
  setApiBase('');
  ok(getSource().apiBase === DEFAULT_API);
  setSource('sumo');
  ok(isSumo());
  setSource('browser');
});

await t('runBody: presets by name, custom sends raw rate, seed integer', () => {
  const b = runBody({ ...sc, density: 'high', seed: 7 }, 'B');
  ok(b.density === 'high' && !('rate' in b) && b.strategy === 'B' && b.seed === 7);
  const c = runBody({ ...sc, density: 'custom', rate: 410.4, blocked: ['J00|J01'] }, 'A');
  ok(c.rate === 410 && c.blocked[0] === 'J00|J01');
});

await t('RemoteSim places vehicles, signals, log and ambulance from frames', () => {
  const s = new RemoteSim(sc);
  ok(s.remote && !s.ambient && s.ambRoute && s.status === 'ready');
  s.apply(frame(1, [car('a', 'W0>J00', 50), car('b', 'J00>J01', 100), { ...car('amb0', 'J00>J01', 0, { kind: 'amb', len: 6.5, prev: 'W0>J00' }) }],
    { log: [{ t: 1, kind: 'request', text: 'x' }] }));
  ok(s.live.length === 3 && s.amb && s.amb.kind === 'amb' && s.amb.inNet);
  for (const v of s.live) ok(Number.isFinite(v.x) && Number.isFinite(v.y) && Number.isFinite(v.h), 'finite pose');
  const sg = s.signals.find((q) => q.id === 'J00');
  ok(sg.phase === 'Y' && sg.greenAxis === 'V' && sg.timer === 1.5);
  ok(s.net.juncById.J00.sig === sg, 'renderer reads net.juncById[..].sig');
  ok(s.logEntries.length === 1 && s.logEntries[0].id === 1);
  // the junction-box vehicle (internal lane) is drawn between the two edges, not on top of the edge start
  const inBox = s.live.find((v) => v.id === 'amb0');
  const onEdge = (() => { const q = new RemoteSim(sc); q.apply(frame(1, [car('z', 'J00>J01', 0)])); return q.live[0]; })();
  ok(Math.hypot(inBox.x - onEdge.x, inBox.y - onEdge.y) > 0.01 || inBox.prevEdge, 'turn pose uses prev edge');
  // second frame: vehicle b leaves, a moves; interpolation start = previous pose
  const ax = s.live.find((v) => v.id === 'a').x;
  s.apply(frame(1.5, [car('a', 'W0>J00', 60)]));
  ok(s.live.length === 1 && s.byId.size === 1);
  const a = s.live[0];
  ok(a.px === ax && a.x !== ax, 'px = previous x, x = new');
  ok(s.ambDone && s.amb.done && !s.amb.inNet, 'ambulance left the network');
  const snap = s.snapshot();
  ok(snap.vehicleCount === 1 && snap.ambDone && snap.metrics.totalQueued === 3 && snap.series.length === 2 && snap.signals.length === 6);
  ok(snap.metrics.throughput === null && snap.backendError === null);
});

await t('RemoteSim playback: queue, pause, speed, finish with SUMO metrics', () => {
  const s = new RemoteSim(sc);
  for (let i = 0; i < 4; i++) s.onMessage(frame(i * 0.5, [car('a', 'W0>J00', 10 * i)]));
  s.onMessage({ type: 'summary', status: 'done', calibration_scale: 0.7, rate_per_entry_veh_h: 231, oversaturated: false, log: [],
    summary: { metrics: { ambFinished: false, ambTravelTime: 300, avgWait: 5, throughput: 1000, violations: 0 } } });
  s.advance(1000, false, 4);
  ok(s.queue.length === 4, 'paused: nothing applied');
  s.advance(1000, true, 4);
  ok(s.queue.length === 3 && s.t === 0);
  s.advance(1100, true, 4);
  ok(s.queue.length === 3, 'not yet due (period 125 ms)');
  s.advance(1130, true, 4);
  ok(s.queue.length === 2);
  ok(s.alphaAt(1130) === 0 || s.alphaAt(1130) < 0.1);
  s.advance(1400, true, 16);
  s.advance(1500, true, 16);
  ok(s.queue.length === 0 && s.finished, 'finishes as soon as the last frame is played and the summary is in');
  ok(s.status === 'done');
  const m = s.snapshot().metrics;
  ok(m.ambTravelTime === null && m.throughput === 1000, 'unfinished ambulance has no travel time');
  ok(s.snapshot().sumo.scale === 0.7 && s.snapshot().sumo.oversaturated === false);
});

await t('RemoteSim errors become a message, never a finished run', () => {
  const s = new RemoteSim(sc);
  s.fail(new BackendError('nope', 'unreachable'));
  const n = s.snapshot();
  ok(n.backendError === 'nope' && !n.finished && n.status === 'error');
  const s2 = new RemoteSim(sc);
  s2.onMessage({ type: 'error', detail: 'boom' });
  ok(/boom/.test(s2.snapshot().backendError));
});

await t('RemoteSim: blocked road with no route -> status no-route, no request needed', () => {
  const s = new RemoteSim({ ...sc, origin: 'W0', dest: 'W0', blocked: [] });
  ok(s.status === 'no-route' && !s.snapshot().hasRoute);
});

await t('unreachable backend gives a plain message naming the address', async () => {
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  setApiBase('http://localhost:8000');
  let err = null;
  try { await startRun(sc, 'C'); } catch (e) { err = e; }
  ok(err instanceof BackendError && err.kind === 'unreachable' && /localhost:8000/.test(err.message) && /Browser mini-simulator/.test(err.message));
});

await t('backend 422 detail is passed through', async () => {
  globalThis.fetch = async () => ({ ok: false, status: 422, json: async () => ({ detail: 'blocked roads leave no route' }) });
  let err = null;
  try { await startRun(sc, 'C'); } catch (e) { err = e; }
  ok(err && /no route/.test(err.message) && err.kind === 'http');
});

await t('runSumoComparison returns the browser runComparison shape, from backend records only', async () => {
  let n = 0;
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    if (opts && opts.method === 'POST') {
      const b = JSON.parse(opts.body);
      calls.push(b);
      return { ok: true, status: 202, json: async () => ({ id: `r${++n}`, strategy: b.strategy, seedUsed: b.seed }) };
    }
    const id = /runs\/(r\d+)/.exec(url)[1];
    const k = Number(id.slice(1));
    const strat = calls[k - 1].strategy;
    return { ok: true, status: 200, json: async () => ({ id, status: 'done', calibration_scale: 0.7, rate_per_entry_veh_h: 231, oversaturated: false, traffic_note: null,
      log: /log=1/.test(url) ? [{ t: 1, kind: 'request', text: 'x', data: {} }] : undefined,
      summary: { metrics: { ambFinished: strat !== 'A' || k > 3, ambTravelTime: 100 + k, ambStops: 1, avgWait: 10, longestWait: 20, maxQueue: 5, throughput: 1000,
        recoveryTime: null, violations: 0 }, safety: {} } }) };
  };
  const prog = [];
  const out = await runSumoComparison({ ...sc, density: 'high' }, ['A', 'B', 'C'], 2, (d, tt) => prog.push(d / tt), () => false);
  ok(calls.length === 6 && calls.every((c) => c.density === 'high' && !('rate' in c)), 'presets by name');
  ok(JSON.stringify(calls.map((c) => c.seed)) === '[7,7,7,8,8,8]', 'consecutive seeds, strategies on the same seed');
  ok(out.seeds.join() === '7,8' && out.results.C.runs.length === 2 && out.results.C.agg);
  ok(out.results.C.runs[0].log.length === 1 && out.results.C.runs[1].log.length === 0, 'log only for the first C run');
  ok(out.results.A.runs[0].metrics.ambTravelTime === null, 'unfinished ambulance has no time');
  ok(out.sumo.calibration_scale === 0.7 && prog[prog.length - 1] === 1);
});

await t('normaliseMetrics keeps finished times', () => {
  ok(normaliseMetrics({ ambFinished: true, ambTravelTime: 90 }).ambTravelTime === 90);
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
