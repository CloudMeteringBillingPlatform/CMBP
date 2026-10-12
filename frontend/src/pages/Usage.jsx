import { useEffect, useMemo, useState } from "react";
import { api, saveBlob } from "../api.js";
import { Card, Empty, ErrorNote, HourlyChart, Spinner } from "../components/ui.jsx";
import { METRIC_INFO, metricValue } from "../format.js";

const RANGES = [
  { id: "24h", label: "Last 24 hours", hours: 24 },
  { id: "48h", label: "Last 48 hours", hours: 48 },
  { id: "7d", label: "Last 7 days", hours: 24 * 7 },
  { id: "30d", label: "Last 30 days", hours: 24 * 30 },
  { id: "period", label: "This billing period" },
  { id: "custom", label: "Custom…" },
];

const toLocalInput = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);

function rangeBounds(rangeId, custom) {
  const end = new Date();
  if (rangeId === "custom") return { start: new Date(custom.start), end: new Date(custom.end) };
  if (rangeId === "period") return { start: new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), 1)), end };
  const hours = RANGES.find((r) => r.id === rangeId).hours;
  return { start: new Date(end.getTime() - hours * 3600_000), end };
}

// Hourly buckets up to 3 days, daily beyond (UTC), so the chart stays readable.
function buckets(rows, start, end) {
  const daily = end - start > 72 * 3600_000;
  const step = daily ? 24 * 3600_000 : 3600_000;
  const floor = (t) => {
    const d = new Date(t);
    if (daily) d.setUTCHours(0, 0, 0, 0);
    else d.setUTCMinutes(0, 0, 0);
    return d.getTime();
  };
  const sums = new Map();
  for (const row of rows) {
    const key = floor(row.hour_start);
    sums.set(key, (sums.get(key) || 0) + Number(row.quantity));
  }
  const points = [];
  const last = floor(end);
  for (let t = floor(start); t <= last; t += step) {
    const d = new Date(t);
    points.push({
      key: String(t),
      label: daily
        ? d.toLocaleDateString("en-IN", { day: "2-digit", month: "short" })
        : d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false }),
      value: sums.get(t) || 0,
      current: t === last,
    });
  }
  return { points, daily };
}

export default function Usage() {
  const [projects, setProjects] = useState([]);
  const [metric, setMetric] = useState("api.requests");
  const [projectId, setProjectId] = useState("");
  const [rangeId, setRangeId] = useState("48h");
  const [custom, setCustom] = useState(() => ({
    start: toLocalInput(new Date(Date.now() - 7 * 24 * 3600_000)),
    end: toLocalInput(new Date()),
  }));
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.get("/api/projects").then((d) => setProjects(d.projects), setError);
  }, []);

  const bounds = useMemo(() => rangeBounds(rangeId, custom), [rangeId, custom]);

  useEffect(() => {
    if (Number.isNaN(bounds.start.getTime()) || Number.isNaN(bounds.end.getTime())) return;
    const params = new URLSearchParams({ start: bounds.start.toISOString(), end: bounds.end.toISOString(), metric });
    if (projectId) params.set("project_id", projectId);
    setData(null);
    api.get(`/api/usage?${params}`).then(
      (d) => {
        setData(d);
        setError(null);
      },
      (err) => {
        setError(err);
        setData({ rows: [] });
      },
    );
  }, [bounds, metric, projectId]);

  const fmt = (v) => metricValue(metric, v);
  const chart = useMemo(() => (data ? buckets(data.rows, bounds.start, bounds.end) : null), [data, bounds]);
  const projectName = (id) => projects.find((p) => p.id === id)?.name ?? `${String(id).slice(0, 8)}…`;

  const byProject = useMemo(() => {
    const totals = new Map();
    for (const row of data?.rows || []) totals.set(row.project_id, (totals.get(row.project_id) || 0) + Number(row.quantity));
    return [...totals.entries()].sort((a, b) => b[1] - a[1]);
  }, [data]);
  const total = byProject.reduce((n, [, v]) => n + v, 0);
  const peak = chart?.points.reduce((best, p) => (p.value > (best?.value ?? -1) ? p : best), null);

  const exportCsv = () => {
    const lines = ["hour_start_utc,project,project_id,metric,quantity"];
    for (const r of data.rows) {
      lines.push([r.hour_start, JSON.stringify(projectName(r.project_id)), r.project_id, r.metric, r.quantity].join(","));
    }
    saveBlob(new Blob([lines.join("\n") + "\n"], { type: "text/csv" }), `usage-${metric}-${rangeId}.csv`);
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Usage explorer</h1>
          <p className="muted">Hourly usage from the counted events, by metric and project, over any range up to 93 days.</p>
        </div>
      </div>

      <Card>
        <div className="inline filters">
          <label>
            Metric
            <select value={metric} onChange={(e) => setMetric(e.target.value)}>
              {Object.entries(METRIC_INFO).map(([k, v]) => (
                <option key={k} value={k}>
                  {v.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Project
            <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              <option value="">All projects</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Range
            <select value={rangeId} onChange={(e) => setRangeId(e.target.value)}>
              {RANGES.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label}
                </option>
              ))}
            </select>
          </label>
          {rangeId === "custom" && (
            <>
              <label>
                From
                <input type="datetime-local" value={custom.start} onChange={(e) => setCustom({ ...custom, start: e.target.value })} />
              </label>
              <label>
                To
                <input type="datetime-local" value={custom.end} onChange={(e) => setCustom({ ...custom, end: e.target.value })} />
              </label>
            </>
          )}
        </div>
      </Card>
      <ErrorNote error={error} />

      {!data ? (
        <Spinner />
      ) : (
        <>
          <div className="grid three">
            <Card title="Total in range">
              <div className="big-number">{fmt(total)}</div>
            </Card>
            <Card title={`Peak ${chart.daily ? "day" : "hour"}`}>
              <div className="big-number">{peak && peak.value > 0 ? fmt(peak.value) : "—"}</div>
              <p className="muted small">{peak && peak.value > 0 ? peak.label : "no usage"}</p>
            </Card>
            <Card title="Projects with usage">
              <div className="big-number">{byProject.length}</div>
            </Card>
          </div>

          <Card
            title={`${METRIC_INFO[metric].label} per ${chart.daily ? "day" : "hour"} (UTC)`}
            actions={
              <button className="ghost small-btn" disabled={!data.rows.length} onClick={exportCsv}>
                Export CSV
              </button>
            }
          >
            <HourlyChart points={chart.points} format={fmt} />
          </Card>

          <Card title="By project">
            {byProject.length === 0 ? (
              <Empty>No usage of this metric in the range.</Empty>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>Project</th>
                    <th className="num">{METRIC_INFO[metric].label}</th>
                    <th className="num">Share</th>
                  </tr>
                </thead>
                <tbody>
                  {byProject.map(([id, value]) => (
                    <tr key={id}>
                      <td>{projectName(id)}</td>
                      <td className="num strong">{fmt(value)}</td>
                      <td className="num muted">{total ? `${Math.round((100 * value) / total)}%` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
