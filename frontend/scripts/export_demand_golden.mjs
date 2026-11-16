// Exports reference data from the REAL browser engine so the Python/SUMO side can be proven identical:
// network layout, random-number outputs, and every ordinary vehicle the engine generates.
//   node scripts/export_demand_golden.mjs
import { writeFileSync } from 'node:fs';
import { Simulation } from '../src/engine/sim.js';
import { buildNetwork, findRoute } from '../src/engine/network.js';
import { makeRng, deriveSeed } from '../src/engine/rng.js';
import { CONFIG } from '../src/engine/config.js';

const net = buildNetwork(CONFIG);
const rng = makeRng(42);
const rngFirst = Array.from({ length: 8 }, () => rng.next());
const derived = [1, 2].map((s) => deriveSeed(7, s));

const scenario = { origin: 'W0', dest: 'E1', density: 'medium', seed: 7, blocked: [] };
const sim = new Simulation(scenario, 'A');
const vehicles = [];
const orig = sim._makeOrdinary.bind(sim);
sim._makeOrdinary = function (e) {
  const q = this.pending[e.id];
  const before = q.length;
  const t = this.t;
  orig(e);
  if (q.length > before) {
    const v = q[q.length - 1];
    const route = v.route;
    vehicles.push({ t: Math.round(t * 1000) / 1000, from: route[0].from, to: route[route.length - 1].to,
      edges: route.map((r) => r.id), speedFactor: v.vmax / CONFIG.carMaxSpeed });
  }
};
for (let i = 0; i < Math.round(CONFIG.maxSimTime / CONFIG.dt); i++) sim.step();

const ambRoute = findRoute(net, 'W0', 'E1', new Set());
const blockedRoute = findRoute(net, 'W0', 'E1', new Set(['J00|J01']));
const golden = {
  config: { dt: CONFIG.dt, warmup: CONFIG.warmup, maxSimTime: CONFIG.maxSimTime, density: CONFIG.density },
  nodes: net.nodeList.map((n) => ({ id: n.id, x: n.x, y: n.y, type: n.type })),
  boundaryIds: net.boundaryIds,
  segments: net.segments.map((s) => ({ id: s.id, a: s.a, b: s.b })),
  edges: net.edges.map((e) => ({ id: e.id, from: e.from, to: e.to, length: e.length, segId: e.segId })),
  entryEdges: net.entryEdges.map((e) => e.id),
  rngFirst, derived,
  route_W0_E1: ambRoute.map((e) => e.id),
  route_W0_E1_blocked_J00_J01: blockedRoute ? blockedRoute.map((e) => e.id) : null,
  vehicles,
};
writeFileSync(new URL('../../backend/tests/golden_js.json', import.meta.url), JSON.stringify(golden));
console.log(`Exported ${net.nodeList.length} nodes, ${net.edges.length} edges, ${vehicles.length} vehicles.`);
