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

// ---------------- Step 10b: several ambulances and live changes (SUMO only) ----------------
const { scenarioAmbulances } = await import('../sumoApi.js');
const flush = () => new Promise((r) => setTimeout(r, 0));
const amb = (id, edge, s, extra = {}) => ({ ...car(id, edge, s), kind: 'amb', len: 6.5, ...extra });

await t('10b: a scenario without the new fields sends exactly the old request body', () => {
  ok(scenarioAmbulances(sc) === null);
  const b = runBody(sc, 'C');
  ok(!('ambulances' in b) && !('live' in b));
  ok(JSON.stringify(Object.keys(runBody(sc, 'C', {})).sort()) === JSON.stringify(Object.keys(b).sort()));
});

await t('10b: ambulance list and live flag are sent only when asked for', () => {
  const two = { ...sc, ambStart: '', extraAmbs: [{ origin: 'N0', dest: 'S0', start: 70 }] };
  const l = scenarioAmbulances(two);
  ok(l.length === 2 && l[0].origin === 'W0' && l[0].start === null && l[1].start === 70);
  const b = runBody(two, 'C', { ambulances: l, live: true });
  ok(b.live === true && b.ambulances.length === 2 && !('start' in b.ambulances[0]) && b.ambulances[1].start === 70);
  ok(!('ambulances' in runBody(two, 'C')), 'Compare and Stress never pass opts, so they stay single-ambulance');
  ok(scenarioAmbulances({ ...sc, ambStart: 90 })[0].start === 90 && scenarioAmbulances({ ...sc, ambStart: 90 }).length === 1);
});

await t('10b: RemoteSim with one ambulance keeps the old behaviour (no tints, no rows)', () => {
  const s = new RemoteSim(sc);
  s.apply(frame(1, [amb('amb0', 'J00>J01', 0, { prev: 'W0>J00' })]));
  ok(s.ambTints === null && s.amb.id === 'amb0');
  s.apply(frame(2, []));
  ok(s.ambDone && s.snapshot().ambCount === 1);
});

await t('10b: three ambulances get colours, the camera follows the first one that is driving, done only when all are', () => {
  const s = new RemoteSim({ ...sc, extraAmbs: [{ origin: 'N0', dest: 'S0', start: 70 }, { origin: 'S1', dest: 'N1', start: 80 }] });
  ok(s.ambCountTotal === 3);
  s.apply(frame(1, [amb('amb0', 'J00>J01', 0), amb('amb1', 'N0>J00', 5), amb('amb2', 'S1>J11', 1)]));
  ok(s.ambTints && Object.keys(s.ambTints).length === 3 && new Set(Object.values(s.ambTints)).size === 3, 'three different colours');
  ok(s.amb.id === 'amb0');
  s.apply(frame(1.5, [amb('amb0', 'J00>J01', 9), amb('amb1', 'N0>J00', 9)]));
  s.apply(frame(2, [amb('amb1', 'N0>J00', 60)]));
  ok(s.amb.id === 'amb1' && !s.ambDone, 'amb0 and amb2 left, amb1 still driving');
  s.apply(frame(5, []));
  ok(s.ambDone, 'all three have left');
  const sn = s.snapshot();
  ok(sn.ambs.length === 3 && sn.ambs.every((a) => a.done) && sn.ambCount === 3);
});

await t('10b: log lines carry the ambulance tag (backend tag, text prefix, or amb0 enter/arrive in a multi run)', () => {
  const s = new RemoteSim({ ...sc, extraAmbs: [{ origin: 'N0', dest: 'S0', start: 70 }] });
  s.apply(frame(1, [], { log: [
    { t: 1, kind: 'request', text: '[amb1] J00: x', data: { amb: 'amb1' } },
    { t: 1, kind: 'request', text: '[amb1] J01: y' },
    { t: 1, kind: 'enter', text: 'Ambulance entered the network at t = 60.0 s.' },
    { t: 1, kind: 'enter', text: 'Ambulance amb1 entered the network at t = 70.0 s.', data: { amb: 'amb1' } },
    { t: 1, kind: 'switch', text: 'J00 signal change' },
  ] }));
  const tags = s.logEntries.map((e) => e.amb);
  ok(JSON.stringify(tags) === JSON.stringify(['amb1', 'amb1', 'amb0', 'amb1', null]), JSON.stringify(tags));
  const one = new RemoteSim(sc);
  one.apply(frame(1, [], { log: [{ t: 1, kind: 'enter', text: 'Ambulance entered the network at t = 60.0 s.' }] }));
  ok(one.logEntries[0].amb === null, 'single-ambulance run: no tags');
});

await t('10b: live traffic change posts to the run, ignores a no-op, and marks the run modified', async () => {
  const posts = [];
  globalThis.fetch = async (url, opts) => {
    posts.push([url, JSON.parse(opts.body)]);
    return { ok: true, status: 200, json: async () => ({ accepted: true }) };
  };
  setApiBase('http://localhost:8000');
  const s = new RemoteSim(sc);
  ok(s.setDensity('high') === false && posts.length === 0, 'no run id yet: nothing sent');
  s.runId = 'r9';
  ok(s.setDensity('medium') === false && posts.length === 0, 'same density: nothing sent, not modified');
  ok(s.setDensity('high') === true);
  await flush();
  ok(posts.length === 1 && /\/api\/runs\/r9\/traffic$/.test(posts[0][0]) && posts[0][1].density === 'high');
  ok(s.modifiedLive === true && s.snapshot().modifiedLive === true);
  ok(s.setDensity('custom', 410.4) === true);
  await flush();
  ok(posts[1][1].rate === 410 && !('density' in posts[1][1]));
  ok(s.setDensity('custom', 410) === false, 'same custom rate: no-op');
  s.finished = true;
  ok(s.setDensity('low') === false && s.canLive === false, 'finished run takes no more changes');
});

await t('10b: a rejected live change shows the backend message and is not hidden', async () => {
  globalThis.fetch = async () => ({ ok: false, status: 409, json: async () => ({ detail: 'the run has already finished' }) });
  const s = new RemoteSim(sc);
  s.runId = 'r9';
  s.setDensity('high');
  await flush();
  ok(/already finished/.test(s.snapshot().liveError));
});

await t('10b: dispatch posts origin and dest, counts up to 3, then refuses', async () => {
  const posts = [];
  let n = 1;
  globalThis.fetch = async (url, opts) => {
    posts.push([url, JSON.parse(opts.body)]);
    n++;
    return { ok: true, status: 200, json: async () => ({ accepted: true, ambulance_id: `amb${n - 1}`, ambulance_count: n }) };
  };
  const s = new RemoteSim(sc);
  ok(s.canDispatch === false && (await s.dispatch('N0', 'S0')) === false && posts.length === 0, 'not live yet');
  s.runId = 'r9';
  ok(s.canDispatch && (await s.dispatch('N0', 'S0')) === true);
  ok(/\/api\/runs\/r9\/dispatch$/.test(posts[0][0]) && posts[0][1].origin === 'N0' && posts[0][1].dest === 'S0');
  ok(s.ambCountTotal === 2 && s.modifiedLive && s.snapshot().ambCount === 2);
  await s.dispatch('S1', 'N1');
  ok(s.ambCountTotal === 3 && s.canDispatch === false && s.snapshot().canDispatch === false);
  ok((await s.dispatch('W1', 'E0')) === false && posts.length === 2, 'cap of 3 is enforced before any request');
});

await t('10b: Compare/Stress requests stay clean (no live flag, no ambulance list)', async () => {
  const bodies = [];
  globalThis.fetch = async (url, opts) => {
    if (opts && opts.method === 'POST') {
      bodies.push(JSON.parse(opts.body));
      return { ok: true, status: 202, json: async () => ({ id: 'q1' }) };
    }
    return { ok: true, status: 200, json: async () => ({ status: 'done', summary: { metrics: { ambFinished: true, ambTravelTime: 80 }, safety: {} } }) };
  };
  const { runSumoOne } = await import('../sumoCompare.js');
  await runSumoOne({ ...sc, extraAmbs: [{ origin: 'N0', dest: 'S0', start: 70 }] }, 'C');
  ok(bodies.length === 1 && !('live' in bodies[0]) && !('ambulances' in bodies[0]));
});

await t('10b: leaving a running SUMO run cancels it on the backend, once; finished or failed runs are not cancelled', async () => {
  const calls = [];
  globalThis.fetch = async (url, opts) => { calls.push(String(url)); return { ok: true, status: 200, json: async () => ({ id: 'r9', status: 'cancelled' }) }; };
  setApiBase('http://localhost:8000');
  const a = new RemoteSim(sc);
  a.runId = 'r9';
  a.dispose();
  await flush();
  ok(calls.length === 1 && /\/api\/runs\/r9\/cancel$/.test(calls[0]), calls.join());
  const done = new RemoteSim(sc);
  done.runId = 'r8';
  done.summary = { metrics: {} };
  done.dispose();
  const failed = new RemoteSim(sc);
  failed.runId = 'r7';
  failed.error = new BackendError('x', 'http');
  failed.dispose();
  const none = new RemoteSim(sc);
  none.dispose();
  await flush();
  ok(calls.length === 1, 'no cancel for a finished run, a failed run, or a run that never got an id');
});

await t('10b: a run whose start request returns after the user restarted is cancelled straight away', async () => {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push([String(url), opts && opts.method]);
    if (/\/cancel$/.test(String(url))) return { ok: true, status: 200, json: async () => ({ status: 'cancelled' }) };
    return { ok: true, status: 202, json: async () => ({ id: 'late1' }) };
  };
  setApiBase('http://localhost:8000');
  const s = new RemoteSim(sc);
  const p = s.start();
  s.dispose();                       // restart while the POST is in flight (runId not known yet, so dispose sends nothing)
  await p;
  await flush();
  ok(calls.some(([u]) => /\/api\/runs\/late1\/cancel$/.test(u)), JSON.stringify(calls));
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
