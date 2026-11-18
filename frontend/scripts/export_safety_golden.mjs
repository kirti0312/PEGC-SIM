// Exports reference data from the REAL browser engine (safety.js, signals.js, strategies.js) so the
// Python safety layer and Strategy B can be proven to behave identically (JS-Python parity, Step 3).
//   node scripts/export_safety_golden.mjs
// Writes ../backend/tests/golden_safety.json.gz (gzip keeps the repeated per-step data small; the Python tests
// read it directly). It only READS the engine; no engine file is changed.
import { writeFileSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { CONFIG } from '../src/engine/config.js';
import { makeSignal, updateSignal, startSwitch } from '../src/engine/signals.js';
import { applyRequest, checkSafety, makeSafetyCounter } from '../src/engine/safety.js';
import { createStrategy, etaFree, fixedReq } from '../src/engine/strategies.js';

const DT = CONFIG.dt;
const PHASE = { G: 0, Y: 1, R: 2 };

function makeJunc(r, c) {
  const j = { id: `J${r}${c}`, r, c, idx: r * CONFIG.cols + c, approach: { H: [{ queue: 0 }], V: [{ queue: 0 }] }, sig: null };
  makeSignal(j, CONFIG);
  return j;
}
const snap = (sig) => [PHASE[sig.phase], sig.greenAxis, sig.timer, sig.switches, sig.emergencyUsed];
const cleanEvents = (events) => events.map((e) => ({ kind: e.kind, text: e.text, rule: e.data.rule }));

// ---------------------------------------------------------------------------------------------
// 1. Single-junction cases: scripted requests go through the real applyRequest / checkSafety.
// ---------------------------------------------------------------------------------------------
function signalCase(name, r, c, seconds, script) {
  const junc = makeJunc(r, c);
  const sig = junc.sig;
  const safety = makeSafetyCounter();
  const sim = { cfg: CONFIG, safety, t: 0, signals: [sig] };
  const steps = [];
  const expected = [];
  const n = Math.round(seconds / DT);
  for (let i = 0; i < n; i++) {
    const t = i * DT;
    sim.t = t;
    updateSignal(sig, CONFIG);
    const s = script(i, t, sig);
    junc.approach.H[0].queue = s.queue.H;
    junc.approach.V[0].queue = s.queue.V;
    let result = null;
    const op = s.op;
    if (op && op.type === 'request') {
      const res = applyRequest(sig, { desired: op.desired, emergency: op.emergency, baselineDesired: op.baselineDesired }, t, CONFIG);
      result = { switched: res.switched, delayed: res.delayed, delayRemaining: res.delayRemaining, events: cleanEvents(res.events) };
    } else if (op && op.type === 'force_switch') startSwitch(sig, t);
    else if (op && op.type === 'set_emergency_used') sig.emergencyUsed = op.value;
    checkSafety(sim);
    steps.push({ queue: s.queue, op: op || null });
    expected.push({ state: snap(sig), result, violations: safety.violations });
  }
  return {
    name, junction: { r, c }, steps, expected,
    final: { violations: safety.violations, byRule: { ...safety.byRule }, lastGreenEnd: { ...sig.lastGreenEnd } },
  };
}

const q = (H, V) => ({ H, V });
const req = (desired, emergency, baselineDesired) => ({ type: 'request', desired, emergency, baselineDesired });
const fixedOp = (sig) => {
  const f = fixedReq(sig, CONFIG);
  return req(f.desired, f.emergency, f.baselineDesired);
};

const signalCases = [
  // normal fixed plan, queues present on both axes (nothing must be flagged)
  signalCase('fixed plan at J00 with queues', 0, 0, 400, (i, t, sig) => ({ queue: q(2, 2), op: fixedOp(sig) })),
  // different initial axis and timer
  signalCase('fixed plan at J12 (V first, timer 15)', 1, 2, 300, (i, t, sig) => ({ queue: q(0, 0), op: fixedOp(sig) })),
  // emergency hold against the normal plan: capped at 25 s, then released
  signalCase('emergency hold cap', 0, 0, 140, (i, t, sig) => ({
    queue: q(3, 3),
    op: t >= 5 ? req(sig.greenAxis === 'H' ? 'H' : 'H', true, 'V') : fixedOp(sig),
  })),
  // an emergency request right after green starts is delayed until minimum green
  signalCase('min green delays an emergency switch', 0, 0, 40, (i, t, sig) => ({ queue: q(0, 0), op: req('V', true, 'H') })),
  // maximum wait overrides an emergency request that wants to keep green
  signalCase('max wait overrides emergency', 0, 0, 160, (i, t, sig) => ({ queue: q(0, 4), op: req('H', true, 'H') })),
  // requests during yellow / all-red are ignored; a non-emergency early request is delayed silently
  signalCase('requests during transition', 0, 0, 40, (i, t, sig) => ({ queue: q(1, 1), op: req('V', false, 'V') })),
  // the independent checker: bypass the safety layer on purpose and check the violations are counted
  signalCase('checker counts violations', 0, 0, 200, (i, t, sig) => {
    let op = null;
    if (i === 12) op = { type: 'force_switch' };            // green only 3 s -> minGreen violation
    if (i === 100) op = { type: 'set_emergency_used', value: 40 }; // holdCap violation (once)
    if (i === 101) op = { type: 'set_emergency_used', value: 0 };
    return { queue: q(5, 5), op };                            // nobody ever switches again -> maxWait violation
  }),
];

// ---------------------------------------------------------------------------------------------
// 2. Strategy B: the real strategyB() against a scripted ambulance, all 6 junctions.
// ---------------------------------------------------------------------------------------------
function strategyBCase(name, ambAxis, juncIds, startD, speed, stopAt, stopSeconds, nQueue, seconds) {
  const juncs = [];
  for (let r = 0; r < CONFIG.rows; r++) for (let c = 0; c < CONFIG.cols; c++) juncs.push(makeJunc(r, c));
  for (const j of juncs) {
    j.approach.H[0].queue = 2;
    j.approach.V[0].queue = 2;
  }
  const byId = Object.fromEntries(juncs.map((j) => [j.id, j]));
  const signals = juncs.map((j) => j.sig);
  const safety = makeSafetyCounter();
  const log = [];
  let upcoming = [];
  const sim = {
    cfg: CONFIG, t: 0, safety, signals,
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
  const strat = createStrategy('B');

  // ambulance state: distance d to the FIRST junction's stop line, then +220 m per further junction
  let d = startD;
  let v = speed;
  let stoppedFor = 0;
  let stopped = false;
  const steps = [];
  const states = [];
  const n = Math.round(seconds / DT);
  for (let i = 0; i < n; i++) {
    const t = i * DT;
    sim.t = t;
    // ambulance state seen by the strategy at the start of this step (from the previous step)
    upcoming = juncIds
      .map((id, k) => ({ junc: byId[id], axis: ambAxis, dStop: d + k * 220, nQueue, eta: 0 }))
      .filter((u) => u.dStop >= -0.5)
      .map((u) => ({ ...u, eta: etaFree(v, Math.max(0, u.dStop), CONFIG) }));
    steps.push({ upcoming: upcoming.map((u) => ({ junction: u.junc.id, axis: u.axis, dStop: u.dStop, nQueue: u.nQueue, eta: u.eta })) });
    for (const sig of signals) updateSignal(sig, CONFIG);
    strat.control(sim);
    checkSafety(sim);
    states.push(signals.map(snap));
    // move the ambulance for the next step: stop at the first stop line for a while, then go on
    if (!stopped && d <= stopAt && stoppedFor === 0) {
      stopped = true;
    }
    if (stopped && stoppedFor < stopSeconds) {
      v = 0;
      stoppedFor += DT;
    } else {
      v = stopped ? 8 : speed;
    }
    d -= v * DT;
    if (d < -40 && juncIds.length > 1) {
      // passed the first junction: the next one becomes the first (shift the origin by one block)
      juncIds = juncIds.slice(1);
      d += 220;
    }
  }
  return { name, steps, states, log, final: { violations: safety.violations, byRule: { ...safety.byRule } } };
}

const bCases = [
  // approaches J00 (green H at start) on the H axis, waits long enough to hit the 25 s hold cap
  strategyBCase('B: J00 along H, long stop (hold cap)', 'H', ['J00', 'J01', 'J02'], 300, 14, 20, 40, 3, 120),
  // approaches J11 on the V axis (J11 starts green H), short stop, minimum-green delay then a switch
  strategyBCase('B: J01 along V, short stop', 'V', ['J01', 'J11'], 260, 12, 15, 6, 0, 90),
  // ambulance never gets close: B must behave exactly like A
  strategyBCase('B: far away (behaves like A)', 'H', ['J00'], 900, 5, -100, 0, 0, 40),
];

// ---------------------------------------------------------------------------------------------
// 3. Rule-based ETA grid.
// ---------------------------------------------------------------------------------------------
const etaGrid = [];
for (const v of [0, 0.5, 3, 8, 12, 14.5, 17, 19.4]) {
  for (const D of [0, 0.5, 5, 20, 45, 80, 120, 150, 220, 400]) etaGrid.push({ v, D, eta: etaFree(v, D, CONFIG) });
}

const golden = {
  config: {
    dt: CONFIG.dt, minGreen: CONFIG.minGreen, yellow: CONFIG.yellow, allRed: CONFIG.allRed,
    maxEmergencyHold: CONFIG.maxEmergencyHold, maxWait: CONFIG.maxWait, lockout: CONFIG.lockout,
    fixedGreen: CONFIG.fixedGreen, bTriggerDistance: CONFIG.bTriggerDistance,
    ambAccel: CONFIG.ambAccel, ambMaxSpeed: CONFIG.ambMaxSpeed, cEtaCruise: CONFIG.cEtaCruise,
  },
  signalCases,
  bCases,
  etaGrid,
};
writeFileSync(new URL('../../backend/tests/golden_safety.json.gz', import.meta.url), gzipSync(JSON.stringify(golden), { level: 9 }));
const steps = signalCases.reduce((a, c) => a + c.steps.length, 0) + bCases.reduce((a, c) => a + c.steps.length, 0);
console.log(`wrote backend/tests/golden_safety.json.gz: ${signalCases.length} signal cases, ${bCases.length} Strategy B cases, ${steps} steps, ${etaGrid.length} ETA points`);
for (const c of signalCases) console.log(`  ${c.name}: violations ${c.final.violations} ${JSON.stringify(c.final.byRule)}`);
for (const c of bCases) console.log(`  ${c.name}: ${c.log.length} log lines, violations ${c.final.violations}`);
