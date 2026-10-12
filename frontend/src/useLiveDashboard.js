import { useEffect, useRef, useState } from "react";
import { api } from "./api.js";

// Live dashboard state (spec section 11, "reconnect without double counting").
//
// On every connect or reconnect:
//   1. open the WebSocket and start buffering usage messages;
//   2. fetch the REST snapshot, which carries the latest sequence number `seq`;
//   3. apply buffered and future messages ONLY if their seq is higher; drop the rest.
// The snapshot already includes every change numbered <= seq, so nothing is counted twice,
// and anything published while the socket was down is covered by the fresh snapshot.

const hourKey = (iso) => new Date(iso).toISOString();

function normalize(snapshot) {
  const totals = {};
  for (const m of snapshot.metrics) totals[m.metric] = m.total;
  const trend = {};
  for (const row of snapshot.trend) {
    const key = hourKey(row.hour_start);
    trend[key] = { ...(trend[key] || {}), [row.metric]: Number(row.quantity) };
  }
  return { ...snapshot, totals, trendMap: trend };
}

function applyUsage(state, msg) {
  if (!state || msg.seq <= state.seq) return state; // already in the snapshot: discard
  const key = hourKey(msg.hour_start);
  const hour = { ...(state.trendMap[key] || {}) };
  hour[msg.metric] = (hour[msg.metric] || 0) + msg.delta;
  const inPeriod = new Date(msg.hour_start) >= new Date(state.period.start);
  return {
    ...state,
    seq: msg.seq,
    totals: inPeriod ? { ...state.totals, [msg.metric]: (state.totals[msg.metric] || 0) + msg.delta } : state.totals,
    trendMap: { ...state.trendMap, [key]: hour },
  };
}

export function useLiveDashboard({ onAlert, onInvoice } = {}) {
  const [state, setState] = useState(null);
  const [status, setStatus] = useState("connecting");
  const [error, setError] = useState(null);
  const handlers = useRef({ onAlert, onInvoice });
  handlers.current = { onAlert, onInvoice };

  useEffect(() => {
    let ws = null;
    let closed = false;
    let retry = 0;
    let current = null;
    let buffer = [];
    let snapshotReady = false;
    let refreshTimer = null;

    const publish = (next) => {
      current = next;
      setState(next);
    };

    async function loadSnapshot() {
      snapshotReady = false;
      buffer = [];
      try {
        let next = normalize(await api.get("/api/dashboard"));
        for (const msg of buffer) next = applyUsage(next, msg);
        buffer = [];
        snapshotReady = true;
        setError(null);
        publish(next);
      } catch (err) {
        setError(err.message);
      }
    }

    function connect() {
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      ws = new WebSocket(`${proto}//${window.location.host}/ws`);
      setStatus("connecting");
      ws.onopen = () => {
        retry = 0;
        setStatus("live");
        loadSnapshot(); // subscribe first, then snapshot
      };
      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === "usage") {
          if (!snapshotReady) buffer.push(msg);
          else publish(applyUsage(current, msg));
        } else if (msg.type === "alert") {
          if (current) publish({ ...current, alerts: [msg, ...current.alerts].slice(0, 10) });
          handlers.current.onAlert?.(msg);
        } else if (msg.type === "invoice") {
          handlers.current.onInvoice?.(msg);
        }
      };
      ws.onclose = () => {
        if (closed) return;
        setStatus("reconnecting");
        snapshotReady = false;
        const delay = Math.min(10_000, 500 * 2 ** retry++);
        setTimeout(() => !closed && connect(), delay);
      };
    }

    connect();
    // The projection and quotas are recomputed server-side; refresh the snapshot each minute.
    refreshTimer = setInterval(() => ws?.readyState === WebSocket.OPEN && loadSnapshot(), 60_000);
    return () => {
      closed = true;
      clearInterval(refreshTimer);
      ws?.close();
    };
  }, []);

  return { state, status, error };
}
