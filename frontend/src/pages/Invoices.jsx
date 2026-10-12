import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api.js";
import { Badge, Card, Empty, ErrorNote, Spinner } from "../components/ui.jsx";
import { dateTime, monthName, rupees } from "../format.js";

export function Invoices() {
  const [invoices, setInvoices] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    api.get("/api/invoices").then((d) => setInvoices(d.invoices), setError);
  }, []);
  if (!invoices) return error ? <ErrorNote error={error} /> : <Spinner />;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Invoices</h1>
          <p className="muted">Issued at the close of each calendar month (UTC). An issued invoice never changes.</p>
        </div>
      </div>
      <Card>
        {invoices.length === 0 ? (
          <Empty>No invoices yet. The first one is issued when this month closes.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Period</th>
                <th>Plan</th>
                <th>Issued</th>
                <th>Adjustments</th>
                <th className="num">Total</th>
              </tr>
            </thead>
            <tbody>
              {invoices.map((inv) => (
                <tr key={inv.id}>
                  <td>
                    <Link to={`/invoices/${inv.id}`}>{monthName(inv.period_start)}</Link>
                  </td>
                  <td className="muted">
                    {inv.plan_id} v{inv.plan_version}
                  </td>
                  <td className="muted">{dateTime(inv.issued_at)}</td>
                  <td>{inv.adjustment_lines > 0 ? <Badge tone="warn">{inv.adjustment_lines} late</Badge> : "—"}</td>
                  <td className="num strong">{rupees(inv.total_paise)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

export function InvoiceDetail() {
  const { id } = useParams();
  const [invoice, setInvoice] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    api.get(`/api/invoices/${id}`).then(setInvoice, setError);
  }, [id]);
  if (!invoice) return error ? <ErrorNote error={error} /> : <Spinner />;
  const usage = invoice.lines.filter((l) => l.kind === "usage");
  const adjustments = invoice.lines.filter((l) => l.kind === "adjustment");

  const Lines = ({ lines }) => (
    <table className="table">
      <thead>
        <tr>
          <th>Description</th>
          <th className="num">Amount</th>
        </tr>
      </thead>
      <tbody>
        {lines.map((l) => (
          <tr key={l.id}>
            <td>{l.description}</td>
            <td className="num">{rupees(l.amount_paise)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <p className="muted small">
            <Link to="/invoices">← Invoices</Link>
          </p>
          <h1>Invoice · {monthName(invoice.period_start)}</h1>
          <p className="muted">
            {invoice.plan_id} plan v{invoice.plan_version} · issued {dateTime(invoice.issued_at)} · <code>{invoice.id}</code>
          </p>
        </div>
        <div className="big-number">{rupees(invoice.total_paise)}</div>
      </div>
      <Card title="Usage lines">{usage.length ? <Lines lines={usage} /> : <Empty>No usage this period.</Empty>}</Card>
      {adjustments.length > 0 && (
        <Card title="Adjustment lines">
          <p className="muted small">
            Late events for earlier, already-closed periods, priced with the plan of the period they belong to.
          </p>
          <Lines lines={adjustments} />
        </Card>
      )}
    </div>
  );
}
