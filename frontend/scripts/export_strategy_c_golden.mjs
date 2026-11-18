// Exports reference data from the REAL browser engine (strategies.js: Strategy C, C1, C2) so the Python
// port can be proven to behave identically (JS-Python parity, Step 4).
//   node scripts/export_strategy_c_golden.mjs
// Writes ../backend/tests/golden_strategy_c.json.gz. It only READS the engine; no engine file is changed.
// Each case drives the real createStrategy(id).control() step by step with a scripted ambulance and
// scripted junction data (queues, moving cars, outgoing occupancy), through the real safety layer.
import { writeFileSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { CONFIG } from '../src/engine/config.js';
import { makeSignal, updateSignal } from '../src/engine/signals.js';
import { applyRequest, checkSafety, makeSafetyCounter } from '../src/engine/safety.js';
import { createStrategy, etaFree } from '../src/engine/strategies.js';

const DT = CONFIG.dt;
const PHASE = { G: 0, Y: 1, R: 2 };
const STOP_LINE = 200;

function makeJunc(r, c) {
  const j = { id: `J${r}${c}`, r, c, idx: r * CONFIG.cols + c, approach: { H: [], V: [] }, out: [], sig: null };
  makeSignal(j, CONFIG);
  return j;
}
const snap = (sig) => [PHASE[sig.phase], sig.greenAxis, sig.timer, sig.switches, sig.emergencyUsed];
const cstate = (sig) => [sig.c.mode, sig.c.recovering, sig.c.lastLogged, sig.ambPassedAt];

// Raw junction data for one step (everything evaluate() and recoveryReq() read), as plain numbers.
// approach[axis] = [{queue, queueStreak, veh: [{d, v, streak}]}] with d = distance to the stop line (m)
// out[axis] = [{length, vehLens: [..]}]
function viewFor(load, spill, idx, bucket) {
  const mk = (axisShift) => [
    {
      queue: (load + idx + bucket + axisShift) % 5,
      queueStreak: ((idx + bucket + axisShift) % 7) * 3.5,
      veh: [
        { d: (5 + bucket * 3 + idx) % 60, v: 0, streak: bucket * 1.5 },
        { d: 12, v: 1.5, streak: 4.25 },
        { d: 47.5, v: (bucket % 3) * 2, streak: idx * 2 },
        { d: -3, v: 0, streak: 99 }, // already past the stop line: must not count
      ],
    },
    { queue: (load * 2 + bucket + axisShift) % 4, queueStreak: ((bucket + axisShift) % 4) * 5, veh: [{ d: 30, v: 4, streak: 0 }, { d: 55, v: 6, streak: 2 }] },
  ];
  const outFor = (axisShift) => ({
    length: 200,
    vehLens: Array.from({ length: spill ? 40 : (idx + bucket + axisShift) % 10 }, () => 4.5),
  });
  return { approach: { H: mk(0), V: mk(1) }, out: { H: [outFor(0)], V: [outFor(1)] } };
}

function applyView(junc, view) {
  for (const axis of ['H', 'V']) {
    junc.approach[axis] = view.approach[axis].map((e) => ({
      queue: e.queue,
      queueStreak: e.queueStreak,
      stopLine: STOP_LINE,
      veh: e.veh.map((v) => ({ s: STOP_LINE - v.d, v: v.v, streak: v.streak, len: 4.5 })),
    }));
  }
  junc.out = [];
  for (const axis of ['H', 'V']) {
    for (const o of view.out[axis]) junc.out.push({ axis, length: o.length, veh: o.vehLens.map((len) => ({ len })) });
  }
}

function strategyCCase(spec) {
  const { name, id, ambAxis, juncIds: ids0, startD, speed, stopAt, stopSeconds, nQueueFn, load, spillFn, recoveryAt, seconds } = spec;
  let juncIds = ids0.slice();
  const juncs = [];
  for (let r = 0; r < CONFIG.rows; r++) for (let c = 0; c < CONFIG.cols; c++) juncs.push(makeJunc(r, c));
  const byId = Object.fromEntries(juncs.map((j) => [j.id, j]));
  const signals = juncs.map((j) => j.sig);
  const safety = makeSafetyCounter();
  const log = [];
  let upcoming = [];
  const sim = {
    cfg: CONFIG, t: 0, safety, signals,
    recovery: { time: null }, totalQueued: 0,
    ambInfo: () => (upcoming === null ? null : { amb: {}, upcoming }),
    requestSignal(sig, rq) {
      const res = applyRequest(sig, rq, sim.t, CONFIG);
      for (const ev of res.events) sim.logEvent(sig, ev.kind, ev.text, ev.data);
      return res;
    },
    logEvent(sig, kind, text, data) {
      log.push({ t: sim.t, junction: sig ? sig.id : null, kind, text, data: data || {} });
    },
  };
  const strat = createStrategy(id);

  const viewTable = [];
  const viewIndex = new Map();
  const viewId = (view) => {
    const key = JSON.stringify(view);
    if (!viewIndex.has(key)) {
      viewIndex.set(key, viewTable.length);
      viewTable.push(view);
    }
    return viewIndex.get(key);
  };

  let d = startD;
  let v = speed;
  let stoppedFor = 0;
  let stopped = false;
  let prevIds = [];
  const steps = [];
  const states = [];
  const cstates = [];
  const n = Math.round(seconds / DT);
  for (let i = 0; i < n; i++) {
    const t = i * DT;
    sim.t = t;
    // data seen by the strategy at the start of this step
    const bucket = Math.floor(t / 3);
    const views = juncs.map((j) => {
      const vw = viewFor(load, spillFn(t), j.idx, bucket);
      applyView(j, vw);
      return viewId(vw);
    });
    sim.recovery.time = recoveryAt !== null && t >= recoveryAt ? 5.5 : null;
    sim.totalQueued = 10 + (Math.floor(t) % 7);
    upcoming = juncIds
      .map((jid, k) => ({ junc: byId[jid], axis: ambAxis[jid] || ambAxis.default, dStop: d + k * 220, nQueue: 0, eta: 0 }))
      .filter((u) => u.dStop >= -0.5)
      .map((u, k) => ({ ...u, nQueue: nQueueFn(t, k), eta: etaFree(v, Math.max(0, u.dStop), CONFIG) }));
    // junctions the ambulance has just cleared: same bookkeeping as Simulation._onAmbPass in sim.js
    const nowIds = upcoming.map((u) => u.junc.id);
    for (const pid of prevIds) {
      if (!nowIds.includes(pid)) {
        const sig = byId[pid].sig;
        sig.ambPassedAt = t;
        sig.c.mode = 'passed';
        sig.c.recovering = true;
        sig.bActive = false;
        sim.logEvent(sig, 'pass', `Ambulance cleared ${sig.id} at t = ${t.toFixed(1)} s.`, { at: t });
      }
    }
    prevIds = nowIds;
    steps.push({
      upcoming: upcoming.map((u) => ({ junction: u.junc.id, axis: u.axis, dStop: u.dStop, nQueue: u.nQueue, eta: u.eta })),
      views,
      recoveryTime: sim.recovery.time,
      totalQueued: sim.totalQueued,
    });
    for (const sig of signals) updateSignal(sig, CONFIG);
    strat.control(sim);
    checkSafety(sim);
    states.push(signals.map(snap));
    cstates.push(signals.map(cstate));
    // move the ambulance for the next step
    if (!stopped && d <= stopAt && stoppedFor === 0) stopped = true;
    if (stopped && stoppedFor < stopSeconds) {
      v = 0;
      stoppedFor += DT;
    } else v = stopped ? 8 : speed;
    d -= v * DT;
    if (d < -40 && juncIds.length > 1) {
      juncIds = juncIds.slice(1);
      d += 220;
    }
  }
  return { name, strategy: id, viewTable, steps, states, cstates, log, final: { violations: safety.violations, byRule: { ...safety.byRule } } };
}

const H = { default: 'H' };
const V = { default: 'V' };
// Row 1 along H (J10 starts V-green, J12 starts V-green): the ambulance's axis is red, so C must prepare / switch.
const common = { ambAxis: H, juncIds: ['J10', 'J11', 'J12'], startD: 420, speed: 14, stopAt: 20, stopSeconds: 12, nQueueFn: () => 3, load: 1, spillFn: () => false, recoveryAt: null, seconds: 150 };
const cases = [
  strategyCCase({ ...common, name: 'C: along H on row 1, queue 3', id: 'C' }),
  strategyCCase({ ...common, name: 'C1: same inputs (no queue awareness)', id: 'C1' }),
  strategyCCase({ ...common, name: 'C2: same inputs (no recovery)', id: 'C2' }),
  strategyCCase({ ...common, name: 'C: recovery ends when queues are back to normal', id: 'C', recoveryAt: 70 }),
  strategyCCase({ ...common, name: 'C: queue ahead changes 0 -> 7 -> 1 during the approach', id: 'C', nQueueFn: (t) => (t < 10 ? 0 : t < 20 ? 7 : 1) }),
  strategyCCase({ ...common, name: 'C: heavy cross traffic (cost exceeds benefit)', id: 'C', load: 4 }),
  strategyCCase({ ...common, name: 'C: spillback on the outgoing road', id: 'C', load: 0, spillFn: () => true }),
  strategyCCase({ ...common, name: 'C: V route down column 0, quiet cross street', id: 'C', load: 0, juncIds: ['J00', 'J10'], ambAxis: V, startD: 380, speed: 10, stopAt: -100, stopSeconds: 0 }),
  strategyCCase({ ...common, name: 'C: V route on green axis (J01, J11), stops at the first line', id: 'C', juncIds: ['J01', 'J11'], ambAxis: V, startD: 260, speed: 12, stopAt: 15, stopSeconds: 6 }),
  strategyCCase({ ...common, name: 'C: ambulance far away (requests stay on the normal plan)', id: 'C', startD: 900, speed: 5, stopAt: -100, stopSeconds: 0, seconds: 40 }),
];

const etaGrid = [];
for (const v of [0, 3, 8, 14.5, 19.4]) for (const D of [0, 5, 45, 150, 400]) etaGrid.push({ v, D, eta: etaFree(v, D, CONFIG) });

const golden = {
  config: {
    dt: CONFIG.dt, minGreen: CONFIG.minGreen, yellow: CONFIG.yellow, allRed: CONFIG.allRed, lockout: CONFIG.lockout,
    fixedGreen: CONFIG.fixedGreen, minGap: CONFIG.minGap, ambAccel: CONFIG.ambAccel, ambMaxSpeed: CONFIG.ambMaxSpeed,
    cEtaCruise: CONFIG.cEtaCruise, cLookaheadJunctions: CONFIG.cLookaheadJunctions, cDischargeHeadway: CONFIG.cDischargeHeadway,
    cMinDelaySaved: CONFIG.cMinDelaySaved, cWeightAmbDelay: CONFIG.cWeightAmbDelay, cCostPerCar: CONFIG.cCostPerCar,
    cWaitWeight: CONFIG.cWaitWeight, cSpillbackPenalty: CONFIG.cSpillbackPenalty, cRecoveryMax: CONFIG.cRecoveryMax,
  },
  cases,
  etaGrid,
};
writeFileSync(new URL('../../backend/tests/golden_strategy_c.json.gz', import.meta.url), gzipSync(JSON.stringify(golden), { level: 9 }));
const total = cases.reduce((a, c) => a + c.steps.length, 0);
console.log(`wrote backend/tests/golden_strategy_c.json.gz: ${cases.length} Strategy C cases, ${total} steps`);
for (const c of cases) {
  const kinds = {};
  for (const e of c.log) kinds[e.kind] = (kinds[e.kind] || 0) + 1;
  console.log(`  ${c.name}: ${c.log.length} log lines ${JSON.stringify(kinds)}, violations ${c.final.violations}`);
}
