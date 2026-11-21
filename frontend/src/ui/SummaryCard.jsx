import { memo } from 'react';
import { ctrl } from './ctrl.js';
import { STRATEGIES, trafficLabel } from '../engine/config.js';
import { useRenderCount } from './hooks.js';
import { ambColor, ambLabel } from '../data/ambColors.js';

const f = (x, d = 1, u = '') => (x === null || x === undefined ? 'not reached' : `${x.toFixed(d)}${u}`);

export const SummaryCard = memo(function SummaryCard({ snap, scenario, onCompare, onClose }) {
  useRenderCount('SummaryCard');
  const m = snap.metrics;
  const s = snap.scenario;
  // Step 10b: a SUMO run with several ambulances reports every one of them (A1's numbers alone would hide the others)
  const amb = Array.isArray(m.ambulances) && m.ambulances.length > 1 ? m.ambulances : null;
  const rows = [
    ...(amb ? [] : [
    ['Ambulance travel time', snap.status === 'no-route' ? 'no route' : m.ambFinished ? f(m.ambTravelTime, 1, ' s') : 'did not arrive in time'],
    ['Ambulance stops', m.ambStops],
    ]),
    ['Avg wait (ordinary)', f(m.avgWait, 1, ' s')],
    ['Longest single wait', f(m.longestWait, 1, ' s')],
    ['Max queue (one approach)', m.maxQueue],
    ['Throughput', `${m.throughput.toFixed(0)} veh/h`],
    ['Recovery time', f(m.recoveryTime, 1, ' s')],
    ['Safety violations', m.violations],
  ];
  return (
    <div className="summary glass" role="dialog" aria-label="Simulation summary">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <h3 style={{ fontSize: 15 }}>Run summary</h3>
        <span className="badge strat">Strategy {snap.strategy} · {STRATEGIES[snap.strategy].name}</span>
      </div>
      {amb && (
        <div className="sum-amb" aria-label="Per-ambulance results">
          <div className="amb-line head"><span /><span>Ambulance</span><span>Travel time</span><span>Delay</span><span>Stops</span></div>
          {amb.map((r) => (
            <div key={r.id} className="amb-line">
              <span className="amb-dot" style={{ background: ambColor(r.id) }} />
              <span className="amb-name">{ambLabel(r.id)}</span>
              <b className="mono">{r.finished ? f(r.travelTime, 1, ' s') : 'did not arrive'}</b>
              <span className="mono">{r.finished ? f(r.delay, 1, ' s') : '—'}</span>
              <span className="mono">{r.stops}</span>
            </div>
          ))}
          {m.ambMeanDelay !== null && m.ambMeanDelay !== undefined && (
            <div className="amb-line agg">
              <span className="amb-name">Mean / worst delay</span>
              <b className="mono">{f(m.ambMeanDelay, 1, ' s')} / {f(m.ambWorstDelay, 1, ' s')}</b>
            </div>
          )}
          {m.ambMeanTravelTime !== null && m.ambMeanTravelTime !== undefined && (
            <div className="amb-line agg">
              <span className="amb-name">Mean / worst travel time</span>
              <b className="mono">{f(m.ambMeanTravelTime, 1, ' s')} / {f(m.ambWorstTravelTime, 1, ' s')}</b>
            </div>
          )}
        </div>
      )}
      <div className="sum-grid">
        {rows.map(([k, v]) => (<div key={k}><span className="dim">{k}</span><b className="mono">{v}</b></div>))}
      </div>
      {amb && <p className="note">Delay is travel time minus an idealised free-flow trip. Recovery time is measured from the first ambulance.{snap.modifiedLive ? ' This run was changed live, so it is not used in clean comparisons.' : ''}</p>}
      <p className="note"><b>Scenario:</b> {s.origin} → {s.dest}, {trafficLabel(s)} traffic{snap.densityChanged ? ' (changed mid-run)' : ''}, seed {s.seed}, blocked roads: {s.blocked.length ? s.blocked.join(', ') : 'none'}. Horizon {m.time.toFixed(0)} s. Browser mini-simulator: not SUMO results.</p>
      <div className="row" style={{ marginTop: 12 }}>
        <button className="btn primary" onClick={() => ctrl.restart()}>Run again</button>
        <button className="btn" onClick={onCompare}>Compare strategies</button>
        <button className="btn" onClick={onClose}>Close</button>
      </div>
      <span hidden>{scenario.seed}</span>
    </div>
  );
});
