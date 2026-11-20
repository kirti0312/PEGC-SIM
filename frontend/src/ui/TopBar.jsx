import { memo, useState } from 'react';
import { STRATEGIES, trafficShort } from '../engine/config.js';
import { NAV, placeLabel } from './places.js';
import { useRenderCount } from './hooks.js';
import { useSource } from '../data/useSource.js';
import { SourceSwitch } from './SourceSwitch.jsx';
import { BROWSER_LABEL, SUMO_LABEL } from '../data/source.js';

function statusInfo(s) {
  if (s.ambient) return { txt: 'Live city · ambient traffic', cls: '' };
  if (s.backendError) return { txt: 'SUMO backend problem', cls: 'bad' };
  if (s.sumo && s.sumo.waiting && !s.finished) return { txt: 'Waiting for SUMO…', cls: 'warn' };
  if (s.status === 'no-route') return { txt: 'No route for the ambulance', cls: 'bad' };
  if (s.finished) return { txt: 'Run finished', cls: 'idle' };
  if (s.status === 'ready') return { txt: 'Ready', cls: 'warn' };
  return s.playing ? { txt: 'Running', cls: '' } : { txt: 'Paused', cls: 'warn' };
}

export const TopBar = memo(function TopBar({ screen, onNav, snap, scenario, onHelp }) {
  useRenderCount('TopBar');
  const [lim, setLim] = useState(false);
  const src = useSource();
  const sumoShown = src.kind === 'sumo' && !snap.ambient; // the ambient city on the Command Centre is always the browser engine
  const st = statusInfo(snap);
  const strat = STRATEGIES[snap.strategy];
  return (
    <header className="topbar">
      <div className="brand">
        <span className="logo" />
        <span>PEGC</span>
        <small>Predictive Emergency Green Corridor</small>
      </div>
      <nav className="nav" aria-label="Screens">
        {NAV.map(([id, label]) => (
          <button key={id} className={screen === id ? 'on' : ''} onClick={() => onNav(id)}>{label}</button>
        ))}
      </nav>
      <div className="top-mid">
        <span className="status" title="Simulation status"><span className={`pulse ${st.cls}`} />{st.txt}</span>
        {!snap.ambient && <span className="badge mono">{scenario.origin} → {scenario.dest} · {trafficShort(scenario)} · seed {scenario.seed}</span>}
        {!snap.ambient && strat && <span className="badge strat" title={strat.name}>Strategy {strat.id} · {strat.short}</span>}
      </div>
      <span className="honest">{sumoShown ? SUMO_LABEL : BROWSER_LABEL}</span>
      <SourceSwitch />
      <button className="cb-btn helpbtn" onClick={onHelp} aria-label="Open the welcome guide" title="Open the welcome guide">?</button>
      <div style={{ position: 'relative' }}>
        <button className="cb-btn limbtn" onClick={() => setLim((v) => !v)} aria-expanded={lim}>Limitations</button>
        {lim && (
          <div className="glass card" style={{ position: 'absolute', right: 0, top: 36, width: 300, background: 'var(--panel-solid)' }}>
            <h3>Limitations</h3>
            {sumoShown ? <p className="note">Real SUMO simulation of the same six-junction grid, one lane per direction, with the backend’s calibrated traffic levels (high traffic is oversaturated by design). The ambulance arrival-time estimate is a rule-based estimate, not machine learning.</p> : <p className="note">Simulated six-junction grid with simplified driver behaviour, one lane per direction, no turning conflicts inside junctions and no real-world validation. The ambulance arrival-time estimate is a rule-based estimate (distance, speed, queue ahead), not machine learning. These are not SUMO results.</p>}
          </div>
        )}
      </div>
    </header>
  );
});
void placeLabel;
