import { memo, useState } from 'react';
import { ctrl } from './ctrl.js';
import { STRATEGIES, CONFIG, DENSITY_CHOICES, entryRate } from '../engine/config.js';
import { SPEEDS, BOUNDARY_IDS, placeLabel } from './places.js';
import { useSource } from '../data/useSource.js';
import { MAX_AMBS } from '../data/ambColors.js';
import { useRenderCount } from './hooks.js';

// Step 10b, SUMO mode only: send another ambulance into the running simulation.
function DispatchGroup({ snap, scenario }) {
  const [origin, setOrigin] = useState(scenario.origin);
  const [dest, setDest] = useState(scenario.dest);
  const ok = snap.canDispatch && origin !== dest;
  return (
    <div className="cb-group">
      <span className="cb-lab">Live</span>
      <select className="cb-select" value={origin} onChange={(e) => setOrigin(e.target.value)} aria-label="Dispatch origin">
        {BOUNDARY_IDS.map((id) => (<option key={id} value={id}>{placeLabel(id)}</option>))}
      </select>
      <span className="cb-lab">→</span>
      <select className="cb-select" value={dest} onChange={(e) => setDest(e.target.value)} aria-label="Dispatch destination">
        {BOUNDARY_IDS.map((id) => (<option key={id} value={id}>{placeLabel(id)}</option>))}
      </select>
      <button className="cb-btn" disabled={!ok} onClick={() => ctrl.dispatchAmbulance(origin, dest)}
        title={snap.ambCount >= MAX_AMBS ? 'At most 3 ambulances per run' : origin === dest ? 'Origin and destination must differ' : 'Send another ambulance now. The run is then marked as changed live and is not used in clean comparisons.'}>
        Dispatch ambulance now ({snap.ambCount}/{MAX_AMBS})
      </button>
      {snap.liveError && <span className="cb-hint" style={{ color: 'var(--bad)' }}>{snap.liveError}</span>}
    </div>
  );
}

export const ControlBar = memo(function ControlBar({ snap, scenario, onStrategy, onLaunch }) {
  useRenderCount('ControlBar');
  const run = snap.mode === 'run';
  const sumoLive = useSource().kind === 'sumo' && run && snap.canLive !== undefined;
  const density = snap.scenario.density;
  return (
    <div className="controlbar glass" role="toolbar" aria-label="Simulation controls">
      <div className="cb-group">
        {run ? (
          <button className="cb-btn on" onClick={() => ctrl.toggle()} disabled={snap.finished} title="Play / pause (space)">
            {snap.playing ? '❚❚ Pause' : '▶ Play'}
          </button>
        ) : (
          <button className="cb-btn primary" onClick={onLaunch}>{'▶'} Launch</button>
        )}
        <button className="cb-btn" onClick={() => ctrl.restart()} title="Restart this scenario with the same seed">{'↺'} Restart</button>
      </div>
      <div className="cb-group">
        <span className="cb-lab">Speed</span>
        {SPEEDS.map((s) => (
          <button key={s} className={`cb-btn ${snap.speed === s ? 'on' : ''}`} onClick={() => ctrl.setSpeed(s)}>{s}×</button>
        ))}
      </div>
      <div className="cb-group">
        <span className="cb-lab">Traffic</span>
        {DENSITY_CHOICES.map((d) => (
          <button key={d} className={`cb-btn ${density === d ? 'on' : ''}`} onClick={() => (d === 'custom' ? ctrl.setDensity('custom', entryRate(snap.scenario)) : ctrl.setDensity(d))}>{d}</button>
        ))}
        {density === 'custom' && (
          <input className="cb-rate" type="number" min={CONFIG.rateMin} max={CONFIG.rateMax} step={10} value={entryRate(snap.scenario)} aria-label="Custom traffic rate, vehicles per hour per entry point" title="Vehicles per hour per entry point"
            onChange={(e) => { const n = Math.round(Number(e.target.value)); if (Number.isFinite(n)) ctrl.setDensity('custom', Math.min(CONFIG.rateMax, Math.max(CONFIG.rateMin, n))); }} />
        )}
      </div>
      {sumoLive && <DispatchGroup snap={snap} scenario={scenario} />}
      <div className="cb-group">
        <span className="cb-lab">Strategy</span>
        <select className="cb-select" value={scenario.strategy} onChange={(e) => onStrategy(e.target.value)} aria-label="Strategy">
          {Object.values(STRATEGIES).map((s) => (
            <option key={s.id} value={s.id}>{s.id} · {s.name}</option>
          ))}
        </select>
      </div>
      <div className="cb-group">
        <button className={`cb-btn ${snap.view === '3d' ? 'on' : ''}`} onClick={() => ctrl.setView('3d')}>3D</button>
        <button className={`cb-btn ${snap.view === '2d' ? 'on' : ''}`} onClick={() => ctrl.setView('2d')}>2D</button>
      </div>
      <div className="cb-group">
        <button className="cb-btn" onClick={() => ctrl.zoomOut()} title="Zoom out" aria-label="Zoom out">{'−'}</button>
        <button className="cb-btn" onClick={() => ctrl.zoomIn()} title="Zoom in" aria-label="Zoom in">+</button>
        <button className="cb-btn" onClick={() => ctrl.fitAll()} title="Smoothly return to the full six-junction overview (key 0)">Overview</button>
        <button className="cb-btn" onClick={() => ctrl.centerAmbulance()} disabled={!run || snap.ambDone} title="Glide to the ambulance (key C)">Center<span className="cb-long"> ambulance</span></button>
        <button className={`cb-btn ${snap.follow ? 'on' : ''}`} onClick={() => ctrl.setFollow(!snap.follow)} disabled={!run} title="Camera follows the ambulance">Follow<span className="cb-long"> ambulance</span></button>
      </div>
      <div className="cb-group">
        <button className="cb-btn" onClick={() => ctrl.setWheelMode(snap.wheelMode === 'zoom' ? 'move' : 'zoom')} title={snap.wheelMode === 'zoom' ? 'The wheel zooms the map. Click to make the wheel and touchpad scroll move the map instead (Ctrl+wheel or pinch always zooms).' : 'The wheel and touchpad scroll move the map. Click to make the wheel zoom instead (Ctrl+wheel or pinch always zooms).'}>Wheel: {snap.wheelMode === 'zoom' ? 'Zoom' : 'Move'}</button>
      </div>
      <span className="cb-hint" title="Move the map: drag with the mouse or use the arrow keys / WASD. Zoom: mouse wheel, + and - keys, or the buttons. 0 or Fit all shows the whole grid. Double-click resets the view.">scroll or drag to move · Ctrl+wheel to zoom</span>
      <span className="cb-seed" title="Random seed: same seed gives identical traffic">seed {snap.scenario.seed}</span>
    </div>
  );
});
