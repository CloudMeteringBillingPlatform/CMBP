import { useMemo, useState } from "react";
import { Badge, Card, ErrorNote, HourlyChart, Meter, Spinner, useToast } from "../components/ui.jsx";
import { METRIC_INFO, bytes, dateTime, hourLabel, metricValue, monthName, rupees } from "../format.js";
import { useLiveDashboard } from "../useLiveDashboard.js";

function lastHours(trendMap, metric, hours = 48) {
  const now = new Date();
  now.setUTCMinutes(0, 0, 0);
  const points = [];
  for (let i = hours - 1; i >= 0; i -= 1) {
    const t = new Date(now.getTime() - i * 3600_000);
    const key = t.toISOString();
    points.push({ key, label: hourLabel(key), value: trendMap[key]?.[metric] || 0, current: i === 0 });
  }
  return points;
}

export default function Dashboard() {
  const toast = useToast();
  const { state, status, error } = useLiveDashboard({
    onAlert: (a) =>
      toast(`${METRIC_INFO[a.metric]?.label || a.metric} reached ${a.threshold}% of the plan allowance`, a.threshold >= 100 ? "danger" : "warn"),
    onInvoice: (i) => toast(`Invoice issued for ${monthName(i.period_start)}: ${rupees(i.total_paise)}`, "info"),
  });
  const [metric, setMetric] = useState("api.requests");
  const points = useMemo(() => (state ? lastHours(state.trendMap, metric) : []), [state, metric]);

  if (!state) return error ? <ErrorNote error={error} /> : <Spinner />;
  const proj = state.projection;
  const statusTone = { live: "ok", connecting: "neutral", reconnecting: "warn" }[status];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p className="muted">
            {monthName(state.period.start)} · {state.plan.name} plan (v{state.plan.version})
          </p>
        </div>
        <Badge tone={statusTone}>{status === "live" ? "● live" : status}</Badge>
      </div>
      <ErrorNote error={error} />

      <div className="grid three">
        <Card title="Bill so far">
          <div className="big-number">{rupees(proj.cost_so_far_paise)}</div>
          <p className="muted small">Rated with your current plan, this period</p>
        </Card>
        <Card title="Projected bill">
          <div className="big-number">{rupees(proj.projected_paise)}</div>
          <p className="muted small">Estimate: usage so far plus the trailing 24-hour run rate</p>
        </Card>
        <Card title="Storage">
          <div className="big-number">{bytes(state.storage.stored_bytes)}</div>
          <Meter value={state.storage.stored_bytes} max={state.storage.cap_bytes} label="storage cap" />
          <p className="muted small">Hard cap {bytes(state.storage.cap_bytes)} · exact, checked on upload</p>
        </Card>
      </div>

      <Card title="Usage this period" actions={<span className="muted small">Soft quotas raise alerts at 80% and 100%</span>}>
        <table className="table">
          <thead>
            <tr>
              <th>Metric</th>
              <th className="num">Used</th>
              <th className="num">Allowance</th>
              <th style={{ width: "30%" }}>Quota</th>
            </tr>
          </thead>
          <tbody>
            {state.metrics.map((m) => {
              const used = state.totals[m.metric] || 0;
              const pct = m.allowance ? Math.round((100 * used) / m.allowance) : null;
              return (
                <tr key={m.metric} className={metric === m.metric ? "selected" : ""} onClick={() => setMetric(m.metric)}>
                  <td>{METRIC_INFO[m.metric]?.label || m.metric}</td>
                  <td className="num strong">{metricValue(m.metric, used)}</td>
                  <td className="num muted">{m.allowance ? metricValue(m.metric, m.allowance) : "—"}</td>
                  <td>
                    {m.allowance ? (
                      <div className="quota">
                        <Meter value={used} max={m.allowance} />
                        <span className="small muted">{pct}%</span>
                      </div>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {state.pending_adjustments?.n > 0 && (
          <p className="muted small">
            {state.pending_adjustments.n} late event(s) for closed periods will appear as adjustment lines on your next invoice.
          </p>
        )}
      </Card>

      <Card
        title={`Hourly trend · ${METRIC_INFO[metric]?.label}`}
        actions={
          <select value={metric} onChange={(e) => setMetric(e.target.value)}>
            {Object.entries(METRIC_INFO).map(([k, v]) => (
              <option key={k} value={k}>
                {v.label}
              </option>
            ))}
          </select>
        }
      >
        <HourlyChart points={points} format={(v) => metricValue(metric, v)} />
        <p className="muted small">Last 48 hours (UTC buckets). Updates live as events are counted.</p>
      </Card>

      <Card title="Recent alerts">
        {state.alerts.length === 0 ? (
          <p className="empty">No alerts this period.</p>
        ) : (
          <ul className="list">
            {state.alerts.map((a) => (
              <li key={`${a.id}-${a.threshold}`}>
                <Badge tone={a.threshold >= 100 ? "danger" : "warn"}>{a.threshold}%</Badge>{" "}
                {METRIC_INFO[a.metric]?.label || a.metric}: {metricValue(a.metric, a.usage)} of{" "}
                {metricValue(a.metric, a.allowance)} <span className="muted small">· {dateTime(a.fired_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
