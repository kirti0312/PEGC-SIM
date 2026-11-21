// Per-ambulance identity (Step 10b): one colour and a short label for amb0..amb2. Used only when a SUMO run has several ambulances.
export const AMB_COLORS = ['#38bdf8', '#f472b6', '#a3e635'];
export const MAX_AMBS = 3;
export const ambIndex = (id) => {
  const n = Number(String(id || '').replace(/^amb/, ''));
  return Number.isInteger(n) && n >= 0 ? n : 0;
};
export const ambColor = (id) => AMB_COLORS[ambIndex(id) % AMB_COLORS.length];
export const ambLabel = (id) => `A${ambIndex(id) + 1}`;
