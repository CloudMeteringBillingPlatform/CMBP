import { createContext, useCallback, useContext, useState } from "react";

export function Card({ title, actions, children, className = "" }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          {title && <h2>{title}</h2>}
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Meter({ value, max, label }) {
  const pct = max > 0 ? Math.min(100, (100 * value) / max) : 0;
  const tone = pct >= 100 ? "danger" : pct >= 80 ? "warn" : "ok";
  return (
    <div className="meter" title={label}>
      <div className={`meter-fill ${tone}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Badge({ tone = "neutral", children }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function ErrorNote({ error }) {
  if (!error) return null;
  return <p className="error">{typeof error === "string" ? error : error.message}</p>;
}

export function Empty({ children }) {
  return <p className="empty">{children}</p>;
}

export function Spinner() {
  return <div className="spinner" aria-label="Loading" />;
}

// ---------------------------------------------------------------- toasts
const ToastContext = createContext(() => {});

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const push = useCallback((text, tone = "info") => {
    const id = Math.random().toString(36).slice(2);
    setToasts((t) => [...t, { id, text, tone }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.tone}`}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext);

// ---------------------------------------------------------------- hourly bar chart (SVG)
export function HourlyChart({ points, format, height = 180 }) {
  const width = 720;
  const pad = { top: 12, right: 8, bottom: 26, left: 8 };
  const max = Math.max(1, ...points.map((p) => p.value));
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const barW = innerW / Math.max(1, points.length);
  const labelEvery = Math.max(1, Math.ceil(points.length / 8));
  return (
    <svg className="chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Hourly usage">
      <line x1={pad.left} x2={width - pad.right} y1={pad.top + innerH} y2={pad.top + innerH} className="axis" />
      {points.map((p, i) => {
        const h = (p.value / max) * innerH;
        return (
          <g key={p.key}>
            <rect
              x={pad.left + i * barW + 1}
              y={pad.top + innerH - h}
              width={Math.max(1, barW - 2)}
              height={h}
              rx={2}
              className={p.current ? "bar current" : "bar"}
            >
              <title>{`${p.label}: ${format(p.value)}`}</title>
            </rect>
            {i % labelEvery === 0 && (
              <text x={pad.left + i * barW + barW / 2} y={height - 8} className="tick" textAnchor="middle">
                {p.label}
              </text>
            )}
          </g>
        );
      })}
      <text x={width - pad.right} y={pad.top + 2} className="tick" textAnchor="end">
        max {format(max)}
      </text>
    </svg>
  );
}
