import { memo } from 'react';
import { Num } from './Num.jsx';
import { LineChart } from './Charts.jsx';
import { useRenderCount } from './hooks.js';
import { CONFIG } from '../engine/config.js';
import { ambColor, ambLabel } from '../data/ambColors.js';

function Kpi({ label, children, cls = '', tag }) {
  return (
    <div className={`kpi ${cls}`}>
      <div className="v">{children}</div>
      <div className="l">{label}{tag && <span className="tag rb">{tag}</span>}</div>
    </div>
  );
}

const f1 = (v) => (v === null || v === undefined ? '—' : `${v.toFixed(1)} s`);

// Step 10b: one row per ambulance (SUMO runs with 2 or more ambulances only).
function AmbRows({ snap, m }) {
  const final = Array.isArray(m.ambulances) ? m.ambulances : null;
  const rows = final || snap.ambs.map((a) => ({ id: a.id, finished: a.done, live: a.active, travelTime: null, delay: null, stops: null }));
  return (
    <div className="amb-table" aria-label="Per-ambulance results">
      <div className="panel-title" style={{ marginBottom: 4 }}>Ambulances</div>
      {rows.map((r) => (
        <div key={r.id} className="amb-line">
          <span className="amb-dot" style={{ background: ambColor(r.id) }} />
          <span className="amb-name">{ambLabel(r.id)}</span>
          <span className="mono">{final ? (r.finished ? f1(r.travelTime) : 'not reached') : r.live ? 'driving' : r.finished ? 'arrived' : 'waiting'}</span>
          <span className="mono dim" title="Delay against an idealised free-flow trip">{final && r.finished ? `delay ${f1(r.delay)}` : ''}</span>
          <span className="mono dim">{final ? `${r.stops} stop${r.stops === 1 ? '' : 's'}` : ''}</span>
        </div>
      ))}
      {final && m.ambMeanDelay !== null && m.ambMeanDelay !== undefined && (
        <div className="amb-line agg">
          <span className="amb-name">Mean / worst delay</span>
          <span className="mono">{f1(m.ambMeanDelay)} / {f1(m.ambWorstDelay)}</span>
        </div>
      )}
    </div>
  );
}

export const AnalyticsPanel = memo(function AnalyticsPanel({ snap, open, onToggle }) {
  useRenderCount('AnalyticsPanel');
  const m = snap.metrics;
  const run = !snap.ambient;
  const noRoute = snap.status === 'no-route';
  const multi = !!(snap.ambs && snap.ambCount > 1);
  let recovery;
  if (m.recoveryTime !== null) recovery = <Num value={m.recoveryTime} suffix=" s" />;
  else if (snap.finished && run) recovery = <span className="bad">not reached</span>;
  else if (snap.ambCleared) recovery = <span className="dim">measuring…</span>;
  else recovery = <span className="dim">—</span>;
  return (
    <aside className={`analytics glass ${open ? '' : 'closed'}`} aria-label="Overall analytics">
      <div className="an-head" onClick={onToggle} role="button" tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && onToggle()}>
        <span className="panel-title">Overall analytics</span>
        <span className="dim mono">{open ? 'sim ' : ''}{open ? <Num value={snap.t} digits={0} suffix=" s" /> : '›'}</span>
      </div>
      {open && (
        <div className="an-body">
          {noRoute && <div className="warnbox">No route exists for this origin, destination and blocked roads. Ordinary traffic still runs; there is no ambulance to measure.</div>}
          {run && !noRoute && (
            <div className="kpis">
              <Kpi label={multi ? `Ambulance time (${ambLabel('amb0')})` : 'Ambulance time'} cls="hi">
                {snap.ambActive || snap.ambDone ? <><Num value={m.ambTravelTime} /><small>s</small></> : <span className="dim">waiting</span>}
              </Kpi>
              <Kpi label="Live ETA to destination" tag="rule-based estimate">
                {snap.ambActive ? <><Num value={snap.ambEta} /><small>s</small></> : <span className="dim">—</span>}
              </Kpi>
              <Kpi label="Ambulance stops"><Num value={snap.ambActive || snap.ambDone ? m.ambStops : null} digits={0} /></Kpi>
              <Kpi label={snap.ambNext ? `Next junction · ${Math.round(snap.ambNext.dStop)} m · queue ${snap.ambNext.nQueue}` : 'Next junction'}>
                {snap.ambNext ? snap.ambNext.junction : <span className="dim">—</span>}
              </Kpi>
            </div>
          )}
          {run && !noRoute && multi && <AmbRows snap={snap} m={m} />}
          <div className="kpis">
            <Kpi label="Avg wait (ordinary)"><Num value={m.avgWait} /><small>s</small></Kpi>
            <Kpi label="Longest single wait"><Num value={m.longestWait} /><small>s</small></Kpi>
            <Kpi label="Queued now"><Num value={m.totalQueued} digits={0} /></Kpi>
            <Kpi label="Max queue (one approach)"><Num value={m.maxQueue} digits={0} /></Kpi>
            <Kpi label="Throughput"><Num value={m.throughput} digits={0} /><small>veh/h</small></Kpi>
            <Kpi label="Vehicles on map"><Num value={snap.vehicleCount} digits={0} /></Kpi>
            {run && <Kpi label="Recovery time">{recovery}</Kpi>}
            {run && <Kpi label="Safety violations" cls={m.violations ? 'badv' : ''}><Num value={m.violations} digits={0} /></Kpi>}
          </div>
          <div>
            <div className="panel-title" style={{ marginBottom: 4 }}>Total queued vehicles</div>
            <LineChart data={snap.series} markX={run && !noRoute ? CONFIG.warmup : null} />
          </div>
          {run && (
            <p className="note">
              <b>Recovery rule</b> (same for every strategy): time after the ambulance leaves its last junction until total queued vehicles stay within {CONFIG.recoveryTolerance} of the pre-ambulance average for {CONFIG.recoverySustain} s.
              {m.recoveryBaseline !== null && <> Baseline this run: {m.recoveryBaseline.toFixed(1)} queued.</>}
            </p>
          )}
          {snap.modifiedLive && <div className="warnbox">Changed live (traffic or an ambulance dispatched during the run): this SUMO run is marked as modified and is not used in clean comparisons.</div>}
          {snap.densityChanged && <div className="warnbox">Traffic density was changed during this run, so these numbers are not comparable with a run at the original density.</div>}
        </div>
      )}
    </aside>
  );
});
