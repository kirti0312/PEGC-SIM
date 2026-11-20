import { memo } from 'react';
import { setSource } from '../data/source.js';
import { ctrl } from './ctrl.js';

// Shown only when the SUMO backend cannot be used (not reachable, run failed, connection lost).
export const BackendBanner = memo(function BackendBanner({ message }) {
  if (!message) return null;
  return (
    <div className="glass card" role="alert" style={{ position: 'fixed', top: 64, left: '50%', transform: 'translateX(-50%)', zIndex: 70, maxWidth: 560, width: 'calc(100% - 32px)' }}>
      <h3>SUMO backend unavailable</h3>
      <p className="note" style={{ margin: '0 0 8px' }}>{message}</p>
      <button className="cb-btn" onClick={() => { setSource('browser'); if (ctrl.mode === 'run') ctrl.restart(); }}>Switch to Browser mini-simulator</button>
    </div>
  );
});
