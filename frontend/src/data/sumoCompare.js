// Compare and Stress Test in SUMO mode: runs the backend one run at a time (the backend queues runs anyway)
// and returns the same shape as engine/runner.js runComparison, so the screens do not change.
// Presets low / medium / high are sent by name: the backend maps them to the calibrated SUMO rates.
import { aggregate } from '../engine/runner.js';
import { startRun, getRun, BackendError } from './sumoApi.js';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// SUMO metrics use the browser's key names. An ambulance that did not finish has no travel time (shown as "not reached").
export function normaliseMetrics(m) {
  const o = { ...m };
  if (!o.ambFinished) o.ambTravelTime = null;
  return o;
}

export async function runSumoOne(scenario, strategy, { isCancelled = () => false, pollMs = 400, withLog = false } = {}) {
  const r = await startRun(scenario, strategy);
  for (;;) {
    if (isCancelled()) throw new BackendError('cancelled', 'run');
    const d = await getRun(r.id, withLog);
    if (d.status === 'error') throw new BackendError(`SUMO run failed: ${d.error || 'unknown error'}`, 'run');
    if (d.status === 'done' && d.summary) return { info: d, run: { strategy, scenario, metrics: normaliseMetrics(d.summary.metrics), log: d.log || [], safety: d.summary.safety } };
    await sleep(pollMs);
  }
}

export async function runSumoComparison(scenario, strategies, seedCount, onProgress, isCancelled) {
  const results = {};
  for (const s of strategies) results[s] = { runs: [] };
  const total = strategies.length * seedCount;
  let done = 0;
  let info = null;
  for (let i = 0; i < seedCount; i++) {
    const seed = Number(scenario.seed) + i;
    for (const s of strategies) {
      const sc = { ...scenario, seed };
      // the decision log is only needed for the first run of Strategy C (Stress Test shows it)
      const { info: d, run } = await runSumoOne(sc, s, { isCancelled, withLog: s === 'C' && i === 0 });
      results[s].runs.push(run);
      info = info || d;
      onProgress && onProgress(++done, total);
    }
  }
  for (const s of strategies) results[s].agg = aggregate(results[s].runs);
  const out = { scenario, seeds: Array.from({ length: seedCount }, (_, i) => Number(scenario.seed) + i), results };
  out.sumo = info && {
    calibration_scale: info.calibration_scale,
    rate_per_entry_veh_h: info.rate_per_entry_veh_h,
    oversaturated: info.oversaturated,
    traffic_note: info.traffic_note,
  };
  return out;
}
