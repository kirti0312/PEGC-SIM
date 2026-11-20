import { memo, useState } from 'react';
import { useSource } from '../data/useSource.js';
import { setSource, setApiBase, DEFAULT_API } from '../data/source.js';
import { health, BackendError } from '../data/sumoApi.js';
import { ctrl } from './ctrl.js';

// Engine switch: Browser mini-simulator (default) or the SUMO backend, with the one API-address setting.
export const SourceSwitch = memo(function SourceSwitch() {
  const src = useSource();
  const [open, setOpen] = useState(false);
  const [addr, setAddr] = useState(src.apiBase);
  const [test, setTest] = useState(null);
  const choose = (k) => {
    if (k === src.kind) return;
    setSource(k);
    if (ctrl.mode === 'run') ctrl.restart(); // reload the current scenario on the other engine
  };
  const check = async () => {
    setApiBase(addr);
    setTest({ txt: 'Checking…' });
    try {
      const h = await health();
      setTest({ txt: `Connected. SUMO ${h.sumo_found ? 'found' : 'NOT found'}; calibration scale ${h.calibration_scale ?? 'missing'}.`, bad: !h.sumo_found || h.calibration_scale == null });
    } catch (e) {
      setTest({ txt: e instanceof BackendError ? e.message : 'Could not check the backend.', bad: true });
    }
  };
  return (
    <div style={{ position: 'relative' }}>
      <button className="cb-btn limbtn" onClick={() => setOpen((v) => !v)} aria-expanded={open} title="Choose which engine produces the numbers">
        Source: {src.kind === 'sumo' ? 'SUMO' : 'Browser'}
      </button>
      {open && (
        <div className="glass card" style={{ position: 'absolute', right: 0, top: 36, width: 320, background: 'var(--panel-solid)', zIndex: 60 }}>
          <h3>Simulation source</h3>
          <div className="seg">
            <button className={src.kind === 'browser' ? 'on' : ''} onClick={() => choose('browser')}>Browser mini-simulator</button>
            <button className={src.kind === 'sumo' ? 'on' : ''} onClick={() => choose('sumo')}>SUMO backend</button>
          </div>
          <p className="note" style={{ margin: '8px 0 6px' }}>
            {src.kind === 'sumo'
              ? 'Runs and comparisons come from the real SUMO simulation on the backend, with the calibrated traffic levels.'
              : 'Runs in this browser tab. Not SUMO results.'}
          </p>
          <label className="note" htmlFor="pegc-api">Backend address</label>
          <input id="pegc-api" className="mono" style={{ width: '100%', boxSizing: 'border-box' }} value={addr} placeholder={DEFAULT_API}
            onChange={(e) => setAddr(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && check()} />
          <div style={{ marginTop: 8 }}>
            <button className="cb-btn" onClick={check}>Save and test connection</button>
          </div>
          {test && <p className={`note ${test.bad ? 'bad' : ''}`} role="status" style={{ margin: '8px 0 0' }}>{test.txt}</p>}
        </div>
      )}
    </div>
  );
});
