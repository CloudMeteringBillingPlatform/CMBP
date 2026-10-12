import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Badge, Card, Empty, ErrorNote, Spinner, useToast } from "../components/ui.jsx";
import { count, dateTime } from "../format.js";

export function DeadLetters() {
  const toast = useToast();
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const load = () => api.get("/api/admin/dead-letters").then((d) => setRows(d.dead_letters), setError);
  useEffect(() => {
    load();
  }, []);

  const retry = async (row) => {
    try {
      const res = await api.post(`/api/admin/dead-letters/${row.id}/retry`);
      toast(`Requeued as ${res.requeued_as}. A retry of an already-counted event changes nothing.`, "info");
      load();
    } catch (err) {
      setError(err);
    }
  };

  if (!rows) return error ? <ErrorNote error={error} /> : <Spinner />;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Dead letters</h1>
          <p className="muted">Events that failed 5 deliveries. They were acknowledged so they cannot block the stream.</p>
        </div>
        <button className="ghost" onClick={load}>
          Refresh
        </button>
      </div>
      <ErrorNote error={error} />
      <Card>
        {rows.length === 0 ? (
          <Empty>No dead letters. Every event so far was processed.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>#</th>
                <th>When</th>
                <th>Error</th>
                <th>Payload</th>
                <th>Tries</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>{r.id}</td>
                  <td className="muted small">{dateTime(r.created_at)}</td>
                  <td className="small error-cell">{r.error}</td>
                  <td>
                    <code className="payload">{JSON.stringify(r.payload)}</code>
                  </td>
                  <td>{r.delivery_count}</td>
                  <td className="num">
                    {r.retried_at ? <Badge>retried</Badge> : null}{" "}
                    <button className="small-btn" onClick={() => retry(r)}>
                      Retry
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

export function SystemStatus() {
  const toast = useToast();
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [close, setClose] = useState({ org_id: "", period: "" });

  const load = () => api.get("/api/admin/status").then(setStatus, setError);
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  const closePeriod = async (e) => {
    e.preventDefault();
    setError(null);
    try {
      const res = await api.post("/api/admin/periods/close", {
        org_id: close.org_id,
        period_start: `${close.period}-01T00:00:00Z`,
      });
      toast(`Period closed; invoice ${res.invoice_id}`, "info");
      load();
    } catch (err) {
      setError(err);
    }
  };

  if (!status) return error ? <ErrorNote error={error} /> : <Spinner />;
  const { stream, counts } = status;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>System status</h1>
          <p className="muted">Refreshes every 5 seconds.</p>
        </div>
      </div>
      <ErrorNote error={error} />
      <div className="grid three">
        <Card title="Stream">
          <div className="big-number">{count(stream.length)}</div>
          <p className="muted small">entries in usage.events (acknowledged entries are trimmed after an hour)</p>
        </Card>
        <Card title="Pending / lag">
          <div className="big-number">
            {count(stream.pending)} / {count(stream.groups.reduce((n, g) => n + (g.lag || 0), 0))}
          </div>
          <p className="muted small">delivered-not-acknowledged / not yet delivered</p>
        </Card>
        <Card title="Dead letters">
          <div className="big-number">{count(counts.dead_letters)}</div>
          <p className="muted small">awaiting retry</p>
        </Card>
      </div>
      <Card title="Consumer groups">
        <table className="table compact">
          <thead>
            <tr>
              <th>Group</th>
              <th className="num">Consumers</th>
              <th className="num">Pending</th>
              <th className="num">Lag</th>
              <th>Last delivered</th>
            </tr>
          </thead>
          <tbody>
            {stream.groups.map((g) => (
              <tr key={g.name}>
                <td>{g.name}</td>
                <td className="num">{g.consumers}</td>
                <td className="num">{g.pending}</td>
                <td className="num">{g.lag ?? "—"}</td>
                <td>
                  <code>{g.last_delivered_id}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted small">
          Counted events: {count(counts.raw_events)} · unapplied adjustments: {count(counts.unapplied_adjustments)} ·
          invoices: {count(counts.invoices)}
        </p>
      </Card>
      <Card title="Close a billing period now">
        <p className="muted small">
          The scheduler closes each month automatically when it ends. This closes one early (for the demo).
        </p>
        <form className="inline" onSubmit={closePeriod}>
          <select value={close.org_id} onChange={(e) => setClose({ ...close, org_id: e.target.value })} required>
            <option value="">Organization…</option>
            {status.orgs.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ))}
          </select>
          <input type="month" value={close.period} onChange={(e) => setClose({ ...close, period: e.target.value })} required />
          <button>Close period</button>
        </form>
      </Card>
    </div>
  );
}
