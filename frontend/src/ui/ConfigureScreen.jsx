import { memo, useEffect, useMemo } from 'react';
import { ctrl } from './ctrl.js';
import { STRATEGIES, defaultScenario } from '../engine/config.js';
import { TrafficPicker } from './TrafficPicker.jsx';
import { previewRoute } from '../engine/runner.js';
import { BOUNDARY_IDS, SEGMENTS, placeLabel } from './places.js';
import { useRenderCount } from './hooks.js';
import { useSource } from '../data/useSource.js';
import { ambColor, ambLabel, MAX_AMBS } from '../data/ambColors.js';

const EXTRA_DEFAULTS = [{ origin: 'N0', dest: 'S0', start: '' }, { origin: 'S1', dest: 'N1', start: '' }];

// Step 10b, SUMO mode only: how many ambulances, and each one's route and start time.
function AmbulancePlan({ scenario, set }) {
  const extra = scenario.extraAmbs || [];
  const count = 1 + extra.length;
  const setCount = (n) => {
    const next = extra.slice(0, n - 1);
    while (next.length < n - 1) next.push({ ...EXTRA_DEFAULTS[next.length] });
    set('extraAmbs', next);
  };
  const setExtra = (i, k, v) => set('extraAmbs', extra.map((a, j) => (j === i ? { ...a, [k]: v } : a)));
  const startInput = (value, onChange, label) => (
    <input type="number" min={0} max={300} step={5} value={value ?? ''} placeholder="after warm-up" aria-label={label} title="Seconds after the run starts (0 to 300). Empty: after the 60 s warm-up."
      onChange={(e) => onChange(e.target.value === '' ? '' : Math.min(300, Math.max(0, Number(e.target.value))))} style={{ width: 110 }} />
  );
  return (
    <div className="field">
      <label>Ambulances in the SUMO run ({count} of {MAX_AMBS})</label>
      <div className="chips">
        {[1, 2, 3].map((n) => (<button key={n} className={`chip ${count === n ? 'on' : ''}`} onClick={() => setCount(n)}>{n}</button>))}
      </div>
      <div className="amb-plan">
        <div className="row amb-row">
          <span className="amb-dot" style={{ background: ambColor('amb0') }} />
          <span className="amb-name">{ambLabel('amb0')}</span>
          <span className="faint">route above, start</span>
          {startInput(scenario.ambStart, (v) => set('ambStart', v), 'Ambulance 1 start time in seconds')}
        </div>
        {extra.map((a, i) => (
          <div key={i} className="row amb-row">
            <span className="amb-dot" style={{ background: ambColor(`amb${i + 1}`) }} />
            <span className="amb-name">{ambLabel(`amb${i + 1}`)}</span>
            <select value={a.origin} onChange={(e) => setExtra(i, 'origin', e.target.value)} aria-label={`Ambulance ${i + 2} origin`}>
              {BOUNDARY_IDS.map((id) => (<option key={id} value={id}>{placeLabel(id)}</option>))}
            </select>
            <span className="faint">→</span>
            <select value={a.dest} onChange={(e) => setExtra(i, 'dest', e.target.value)} aria-label={`Ambulance ${i + 2} destination`}>
              {BOUNDARY_IDS.map((id) => (<option key={id} value={id}>{placeLabel(id)}</option>))}
            </select>
            {startInput(a.start, (v) => setExtra(i, 'start', v), `Ambulance ${i + 2} start time in seconds`)}
          </div>
        ))}
      </div>
      <p className="note" style={{ marginTop: 6 }}>SUMO only. Compare and Stress Test always use one ambulance. You can also send another ambulance while the run is going (Dispatch ambulance now).</p>
    </div>
  );
}

export const ConfigureScreen = memo(function ConfigureScreen({ scenario, setScenario, onLaunch }) {
  useRenderCount('ConfigureScreen');
  const set = (k, v) => setScenario({ ...scenario, [k]: v });
  const route = useMemo(() => previewRoute(scenario), [scenario.origin, scenario.dest, scenario.blocked]);
  const same = scenario.origin === scenario.dest;
  const sumo = useSource().kind === 'sumo';
  const extraSame = sumo && (scenario.extraAmbs || []).some((a) => a.origin === a.dest);

  // live preview on the map
  useEffect(() => {
    ctrl.setOverlay({ routes: route ? [{ nodes: route, style: 'main' }] : [], blocked: scenario.blocked, origin: scenario.origin, dest: scenario.dest });
    return () => ctrl.clearOverlay();
  }, [route, scenario.blocked, scenario.origin, scenario.dest]);
  useEffect(() => {
    ctrl.fitAll();
  }, []);

  const toggleBlock = (id) => set('blocked', scenario.blocked.includes(id) ? scenario.blocked.filter((x) => x !== id) : [...scenario.blocked, id]);
  const msg = same ? 'Origin and destination must differ.' : extraSame ? 'Every ambulance needs an origin and a destination that differ.' : !route ? 'No route exists for this origin, destination and set of blocked roads. The simulation will run ordinary traffic only and report that there is no route.' : null;
  const sel = (key, label) => (
    <div className="field">
      <label>{label}</label>
      <select value={scenario[key]} onChange={(e) => set(key, e.target.value)}>
        {BOUNDARY_IDS.map((id) => (<option key={id} value={id}>{placeLabel(id)}</option>))}
      </select>
    </div>
  );
  return (
    <div className="screen over">
      <div className="side glass card" style={{ height: 'calc(100% - 6px)', overflow: 'auto' }}>
        <h2>Configure scenario</h2>
        <p className="note" style={{ margin: '6px 0 14px' }}>One scenario object is reused by every strategy, so comparisons use the same network, traffic, route, blocked roads and seed.</p>
        {sel('origin', 'Ambulance origin')}
        {sel('dest', 'Ambulance destination')}
        {sumo && <AmbulancePlan scenario={scenario} set={set} />}
        <div className="field">
          <label>Traffic density (vehicles/h per entry point)</label>
          <TrafficPicker value={{ density: scenario.density, rate: scenario.rate }} onChange={(v) => setScenario({ ...scenario, ...v })} />
        </div>
        <div className="field">
          <label>Random seed</label>
          <div className="row">
            <input type="number" value={scenario.seed} min={1} step={1} onChange={(e) => set('seed', Math.max(1, Math.floor(Number(e.target.value) || 1)))} style={{ width: 100 }} />
            <button className="btn" onClick={() => set('seed', 1 + Math.floor(Math.random() * 99999))}>Random</button>
          </div>
        </div>
        <div className="field">
          <label>Strategy to watch</label>
          <select value={scenario.strategy} onChange={(e) => set('strategy', e.target.value)}>
            {Object.values(STRATEGIES).map((s) => (<option key={s.id} value={s.id}>{s.id} · {s.name}</option>))}
          </select>
        </div>
        <div className="field">
          <label>Blocked roads ({scenario.blocked.length})</label>
          <div className="chips">
            {SEGMENTS.map((s) => (<button key={s.id} className={`chip ${scenario.blocked.includes(s.id) ? 'on' : ''}`} onClick={() => toggleBlock(s.id)}>{s.label}</button>))}
          </div>
        </div>
        {msg && <div className="warnbox" style={{ marginBottom: 12 }}>{msg}</div>}
        <div className="row">
          <button className="btn primary" onClick={onLaunch}>Launch with these settings</button>
          <button className="btn" onClick={() => setScenario({ ...defaultScenario() })}>Reset</button>
        </div>
      </div>
    </div>
  );
});
