// Step 0: regression baseline for the browser engine.
//   node scripts/baseline.mjs --write   save current results to baseline.json
//   node scripts/baseline.mjs --check   re-run and require an exact match
// Run --write once on a clean checkout, and --check after any engine change.
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { runScenario } from '../src/engine/runner.js';

const STRATEGIES = ['A', 'B', 'C', 'C1', 'C2'];
const DENSITIES = ['low', 'medium', 'high'];
const SEEDS = [1, 2, 3];
const FILE = new URL('../baseline.json', import.meta.url);

function collect() {
  const out = {};
  for (const strategy of STRATEGIES) {
    for (const density of DENSITIES) {
      for (const seed of SEEDS) {
        const scenario = { origin: 'W0', dest: 'E1', density, seed, blocked: [] };
        const res = runScenario(scenario, strategy);
        out[`${strategy}|${density}|${seed}`] = { metrics: res.metrics, violations: res.safety.violations };
      }
    }
  }
  return out;
}

const mode = process.argv[2];
const now = collect();
if (mode === '--write') {
  writeFileSync(FILE, JSON.stringify(now, null, 1));
  console.log(`Wrote baseline.json (${Object.keys(now).length} runs).`);
} else if (mode === '--check') {
  if (!existsSync(FILE)) { console.error('baseline.json not found. Run with --write first.'); process.exit(2); }
  const saved = JSON.parse(readFileSync(FILE, 'utf8'));
  let bad = 0;
  for (const k of Object.keys(saved)) {
    if (JSON.stringify(saved[k]) !== JSON.stringify(JSON.parse(JSON.stringify(now[k])))) { bad++; console.error('DIFFERENT:', k); }
  }
  console.log(bad ? `FAILED: ${bad} of ${Object.keys(saved).length} runs differ.` : `OK: all ${Object.keys(saved).length} runs match the baseline exactly.`);
  process.exit(bad ? 1 : 0);
} else {
  console.log('Usage: node scripts/baseline.mjs --write | --check');
}
