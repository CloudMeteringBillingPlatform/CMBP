import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Badge, Card, ErrorNote, Spinner, useToast } from "../components/ui.jsx";
import { METRIC_INFO, bytes, count, dateTime, metricValue } from "../format.js";

function rateText(card) {
  const unit = card.unit_label;
  if (card.tiers) {
    return card.tiers
      .map((t, i, all) => {
        const lower = i === 0 ? 0 : all[i - 1].up_to;
        const range = t.up_to == null ? `over ${count(lower)}` : `${count(lower)}–${count(t.up_to)}`;
        return `${range}: ${t.rate === 0 ? "free" : `₹${(t.rate / 100).toFixed(2)} / ${unit}`}`;
      })
      .join(" · ");
  }
  return `₹${(card.rate / 100).toFixed(2)} / ${unit}`;
}

function PlanCard({ version, current, onSwitch, busy }) {
  const { limits, rates } = version;
  return (
    <Card
      className={current ? "plan current" : "plan"}
      title={
        <>
          {version.name} <span className="muted small">v{version.version}</span> {current && <Badge tone="info">current</Badge>}
        </>
      }
      actions={!current && <button disabled={busy} onClick={() => onSwitch(version.plan_id)}>Switch to {version.name}</button>}
    >
      <div className="limits">
        <div>
          <div className="muted small">Rate limit</div>
          <div className="strong">{count(limits.rate_limit_per_minute)} req/min per key</div>
        </div>
        <div>
          <div className="muted small">Storage cap</div>
          <div className="strong">{bytes(limits.storage_bytes_cap)}</div>
        </div>
      </div>
      <table className="table compact">
        <thead>
          <tr>
            <th>Metric</th>
            <th>Included / quota</th>
            <th>Price</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(rates).map(([metric, card]) => (
            <tr key={metric}>
              <td>{METRIC_INFO[metric]?.label || metric}</td>
              <td className="muted">
                {card.included ? metricValue(metric, card.included) : "—"}
                {limits.quotas?.[metric] ? ` / ${metricValue(metric, limits.quotas[metric])}` : ""}
              </td>
              <td className="small">{rateText(card)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">Effective from {dateTime(version.effective_from)} · published versions never change</p>
    </Card>
  );
}

export default function Plan() {
  const toast = useToast();
  const [plans, setPlans] = useState(null);
  const [sub, setSub] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    const [p, s] = await Promise.all([api.get("/api/plans"), api.get("/api/subscription")]);
    setPlans(p.plan_versions);
    setSub(s);
  };
  useEffect(() => {
    load().catch(setError);
  }, []);

  const switchTo = async (planId) => {
    if (!window.confirm("Switch plan? New limits apply immediately; this month is billed on the plan you end it on.")) return;
    setBusy(true);
    try {
      await api.put("/api/subscription", { plan_id: planId });
      toast("Plan switched", "info");
      await load();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  if (!plans || !sub) return error ? <ErrorNote error={error} /> : <Spinner />;
  // Show each plan's version in effect now (the newest one that has started).
  const inEffect = Object.values(
    plans.filter((v) => v.in_effect).reduce((acc, v) => ({ ...acc, [v.plan_id]: v }), {}),
  );
  const upcoming = plans.filter((v) => !v.in_effect);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Plan</h1>
          <p className="muted">
            You are on <strong>{sub.current.plan_name}</strong> (version {sub.current.version}).
          </p>
        </div>
      </div>
      <ErrorNote error={error} />
      <div className="grid two">
        {inEffect.map((v) => (
          <PlanCard
            key={`${v.plan_id}-${v.version}`}
            version={v}
            current={v.plan_id === sub.current.plan_id}
            onSwitch={switchTo}
            busy={busy}
          />
        ))}
      </div>
      {upcoming.length > 0 && (
        <Card title="Upcoming price versions">
          <ul className="list">
            {upcoming.map((v) => (
              <li key={`${v.plan_id}-${v.version}`}>
                {v.name} v{v.version} takes effect {dateTime(v.effective_from)}
              </li>
            ))}
          </ul>
        </Card>
      )}
      <Card title="Plan history">
        <ul className="list">
          {sub.history.map((h) => (
            <li key={h.effective_from}>
              <strong>{h.plan_id}</strong> <span className="muted">from {dateTime(h.effective_from)}</span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
