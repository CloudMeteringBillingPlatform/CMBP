export const METRIC_INFO = {
  "api.requests": { label: "API requests", kind: "count" },
  "storage.bytes_in": { label: "Storage ingress", kind: "bytes" },
  "storage.bytes_out": { label: "Storage egress", kind: "bytes" },
  "storage.byte_hours": { label: "Storage held", kind: "byte-hours" },
  "image.bytes_processed": { label: "Image bytes processed", kind: "bytes" },
  "image.cpu_seconds": { label: "Image CPU time", kind: "cpu-ms" },
};

export function bytes(n) {
  if (n == null) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  let v = Number(n);
  while (Math.abs(v) >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 2 : i > 0 ? 1 : 0)} ${units[i]}`;
}

export function count(n) {
  return n == null ? "—" : Number(n).toLocaleString("en-IN");
}

export function metricValue(metric, n) {
  const kind = METRIC_INFO[metric]?.kind;
  if (kind === "bytes") return bytes(n);
  if (kind === "byte-hours") return `${bytes(n)}·h`;
  if (kind === "cpu-ms") return `${(Number(n) / 1000).toLocaleString("en-IN", { maximumFractionDigits: 2 })} CPU-s`;
  return count(n);
}

export function rupees(paise) {
  if (paise == null) return "—";
  return `₹${(Number(paise) / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function dateTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

export function monthName(iso) {
  return new Date(iso).toLocaleString("en-IN", { month: "long", year: "numeric", timeZone: "UTC" });
}

export function hourLabel(iso) {
  return new Date(iso).toLocaleString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false });
}
