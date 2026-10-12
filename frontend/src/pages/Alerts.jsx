import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Badge, Card, Empty, ErrorNote, Spinner } from "../components/ui.jsx";
import { METRIC_INFO, dateTime, metricValue, monthName } from "../format.js";

export default function Alerts() {
  const [alerts, setAlerts] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    api.get("/api/alerts").then((d) => setAlerts(d.alerts), setError);
  }, []);
  if (!alerts) return error ? <ErrorNote error={error} /> : <Spinner />;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Alerts</h1>
          <p className="muted">
            Each threshold fires once per metric per period. Soft quotas are checked every minute, so they can lag
            usage slightly; hard limits (rate limit, storage cap) are exact.
          </p>
        </div>
      </div>
      <Card>
        {alerts.length === 0 ? (
          <Empty>No alerts have fired.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Fired</th>
                <th>Period</th>
                <th>Metric</th>
                <th>Threshold</th>
                <th className="num">Usage / allowance</th>
              </tr>
            </thead>
            <tbody>
              {alerts.map((a) => (
                <tr key={a.id}>
                  <td className="muted">{dateTime(a.fired_at)}</td>
                  <td>{monthName(a.period_start)}</td>
                  <td>{METRIC_INFO[a.metric]?.label || a.metric}</td>
                  <td>
                    <Badge tone={a.threshold >= 100 ? "danger" : "warn"}>{a.threshold}%</Badge>
                  </td>
                  <td className="num">
                    {metricValue(a.metric, a.usage)} / {metricValue(a.metric, a.allowance)}
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
