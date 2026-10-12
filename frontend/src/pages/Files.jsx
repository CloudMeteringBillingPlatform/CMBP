import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { gw, objectName, saveBlob } from "../api.js";
import { KeyGate, useApiKey } from "../apiKey.jsx";
import { Badge, Card, Empty, ErrorNote, Spinner, useToast } from "../components/ui.jsx";
import { bytes, dateTime } from "../format.js";

const MAX_OBJECT_BYTES = 10 * 1024 * 1024;

function kindOf(type) {
  if (type?.startsWith("image/")) return "image";
  if (type === "application/pdf") return "pdf";
  if (type?.startsWith("text/")) return "text";
  return "file";
}

function Uploads({ items }) {
  if (items.length === 0) return null;
  return (
    <ul className="uploads">
      {items.map((u) => (
        <li key={u.id}>
          <span className="upload-name">{u.name}</span>
          <span className="muted small">{bytes(u.size)}</span>
          {u.state === "uploading" && (
            <div className="meter">
              <div className="meter-fill ok" style={{ width: `${Math.round(u.progress * 100)}%` }} />
            </div>
          )}
          {u.state === "done" && <Badge tone="ok">stored</Badge>}
          {u.state === "error" && <Badge tone="danger">{u.error}</Badge>}
        </li>
      ))}
    </ul>
  );
}

function FileBrowser() {
  const toast = useToast();
  const { apiKey, setApiKey } = useApiKey();
  const [objects, setObjects] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState("");
  const [uploads, setUploads] = useState([]);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(null);
  const input = useRef(null);

  // A key revoked while in use: drop it so the page asks for another.
  const fail = useCallback(
    (err) => {
      if (err.status === 401) setApiKey(null);
      else setError(err);
    },
    [setApiKey],
  );

  const load = useCallback(async () => {
    try {
      setObjects(await gw.list(apiKey));
      setError(null);
    } catch (err) {
      fail(err);
    }
  }, [apiKey, fail]);

  useEffect(() => {
    setObjects(null);
    load();
  }, [load]);

  const patchUpload = (id, patch) => setUploads((list) => list.map((u) => (u.id === id ? { ...u, ...patch } : u)));

  const uploadFiles = async (files) => {
    const queued = Array.from(files).map((file) => ({
      id: Math.random().toString(36).slice(2),
      file,
      name: objectName(file.name) || "file",
      size: file.size,
      progress: 0,
      state: "uploading",
    }));
    setUploads((list) => [...queued, ...list].slice(0, 12));
    for (const u of queued) {
      if (u.size > MAX_OBJECT_BYTES) {
        patchUpload(u.id, { state: "error", error: "over 10 MB" });
        continue;
      }
      try {
        await gw.upload(apiKey, u.name, u.file, (p) => patchUpload(u.id, { progress: p }));
        patchUpload(u.id, { state: "done", progress: 1 });
      } catch (err) {
        patchUpload(u.id, { state: "error", error: err.status === 429 ? "storage full or rate limited" : err.message });
      }
    }
    load();
  };

  const download = async (o) => {
    setBusy(o.name);
    try {
      saveBlob(await gw.download(apiKey, o.name), o.name);
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const open = async (o) => {
    // Open the tab synchronously (popup blockers), then point it at the downloaded blob.
    const tab = window.open("", "_blank");
    setBusy(o.name);
    try {
      const blob = await gw.download(apiKey, o.name);
      const url = URL.createObjectURL(new Blob([blob], { type: o.content_type }));
      if (tab) tab.location.href = url;
      else window.location.href = url;
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (err) {
      tab?.close();
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const remove = async (o) => {
    if (!window.confirm(`Delete ${o.name}? This cannot be undone.`)) return;
    try {
      await gw.remove(apiKey, o.name);
      toast(`Deleted ${o.name}`, "info");
      load();
    } catch (err) {
      fail(err);
    }
  };

  const shown = (objects || []).filter((o) => o.name.toLowerCase().includes(filter.toLowerCase()));
  const total = (objects || []).reduce((n, o) => n + Number(o.size_bytes), 0);

  return (
    <>
      <ErrorNote error={error} />

      <div
        className={`dropzone ${dragging ? "over" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          uploadFiles(e.dataTransfer.files);
        }}
        onClick={() => input.current?.click()}
      >
        <input
          ref={input}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            uploadFiles(e.target.files);
            e.target.value = "";
          }}
        />
        <div className="strong">Drop files here or click to choose</div>
        <div className="muted small">Any type, up to 10 MB each. Names are made URL-safe: “My Report.pdf” → my-report.pdf</div>
      </div>
      <Uploads items={uploads} />

      <Card
        title={`${objects ? objects.length : "…"} object(s) · ${bytes(total)}`}
        actions={<input placeholder="Filter by name" value={filter} onChange={(e) => setFilter(e.target.value)} />}
      >
        {!objects ? (
          <Spinner />
        ) : shown.length === 0 ? (
          <Empty>{objects.length ? "No objects match the filter." : "No objects yet. Upload one above."}</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th className="num">Size</th>
                <th>Uploaded</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {shown.map((o) => {
                const kind = kindOf(o.content_type);
                return (
                  <tr key={o.name}>
                    <td className="strong break">{o.name}</td>
                    <td className="muted small">{o.content_type}</td>
                    <td className="num">{bytes(o.size_bytes)}</td>
                    <td className="muted small">{dateTime(o.created_at)}</td>
                    <td className="num actions">
                      {(kind === "image" || kind === "pdf" || kind === "text") && (
                        <button className="ghost small-btn" disabled={busy === o.name} onClick={() => open(o)}>
                          Open
                        </button>
                      )}
                      {kind === "image" && (
                        <Link className="small-link" to={`/images?object=${encodeURIComponent(o.name)}`}>
                          Process
                        </Link>
                      )}
                      <button className="ghost small-btn" disabled={busy === o.name} onClick={() => download(o)}>
                        Download
                      </button>
                      <button className="danger small-btn" onClick={() => remove(o)}>
                        Delete
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}

export default function Files() {
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Files</h1>
          <p className="muted">
            Object storage for the project your API key belongs to. Every upload, download and delete goes through the
            public gateway and is metered like any API call.
          </p>
        </div>
      </div>
      <KeyGate purpose="Browse, upload, download and delete stored objects.">
        <FileBrowser />
      </KeyGate>
    </div>
  );
}
