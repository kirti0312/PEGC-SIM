import { memo } from 'react';
import { CONFIG, DENSITY_CHOICES, entryRate } from '../engine/config.js';
import { ENTRY_COUNT } from './places.js';
import { useSource } from '../data/useSource.js';

// Low / Medium / High presets plus a Custom rate (vehicles per hour per entry point).
// value: { density, rate }. onChange({ density, rate }).
export const TrafficPicker = memo(function TrafficPicker({ value, onChange }) {
  const sumo = useSource().kind === 'sumo';
  const custom = value.density === 'custom';
  const rate = entryRate(value);
  const setRate = (r) => {
    const n = Math.round(Number(r));
    onChange({ density: 'custom', rate: Number.isFinite(n) ? Math.min(CONFIG.rateMax, Math.max(CONFIG.rateMin, n)) : rate });
  };
  return (
    <div>
      <div className="seg">
        {DENSITY_CHOICES.map((d) => (
          <button key={d} className={value.density === d ? 'on' : ''} onClick={() => onChange(d === 'custom' ? { density: 'custom', rate } : { density: d, rate: value.rate })}>{d}</button>
        ))}
      </div>
      {!custom && <div className="note" style={{ marginTop: 4 }}>{sumo ? `${value.density}: calibrated SUMO preset (the backend sets the rate; it is shown once a run starts)` : `${value.density}: ${CONFIG.density[value.density]} veh/h per entry point`}</div>}
      {custom && sumo && <div className="note" style={{ marginTop: 4 }}>custom: raw rate, not calibrated; above about 240 per entry may saturate</div>}
      {custom && (
        <div className="rate-box">
          <input type="range" min={CONFIG.rateMin} max={CONFIG.rateMax} step={10} value={rate} onChange={(e) => setRate(e.target.value)} aria-label="Custom traffic rate, vehicles per hour per entry point" />
          <div className="row">
            <input type="number" min={CONFIG.rateMin} max={CONFIG.rateMax} step={10} value={rate} onChange={(e) => setRate(e.target.value)} style={{ width: 84 }} aria-label="Custom rate number" />
            <span className="note">veh/h per entry × {ENTRY_COUNT} entries = <b>{(rate * ENTRY_COUNT).toLocaleString()}</b> veh/h total (allowed {CONFIG.rateMin}–{CONFIG.rateMax})</span>
          </div>
        </div>
      )}
    </div>
  );
});
