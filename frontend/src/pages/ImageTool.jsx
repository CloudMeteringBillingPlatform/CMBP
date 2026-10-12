import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { gw, objectName, saveBlob } from "../api.js";
import { KeyGate, useApiKey } from "../apiKey.jsx";
import { Card, ErrorNote, useToast } from "../components/ui.jsx";
import { bytes } from "../format.js";

const MAX_INPUT = 2 * 1024 * 1024;
const PRESETS = [
  { label: "Thumbnail", width: 150, height: "", format: "webp" },
  { label: "Avatar 256²", width: 256, height: 256, format: "png" },
  { label: "Web 800px", width: 800, height: "", format: "jpeg" },
  { label: "Convert to WebP", width: "", height: "", format: "webp" },
];
const EXT = { png: "png", jpeg: "jpg", webp: "webp" };

function useObjectUrl(blob) {
  const url = useMemo(() => (blob ? URL.createObjectURL(blob) : null), [blob]);
  useEffect(() => () => url && URL.revokeObjectURL(url), [url]);
  return url;
}

function Preview({ title, blob, extra }) {
  const url = useObjectUrl(blob);
  const [dims, setDims] = useState(null);
  useEffect(() => setDims(null), [url]);
  return (
    <Card title={title} className="preview-card">
      {url ? (
        <>
          <div className="preview">
            <img src={url} alt={title} onLoad={(e) => setDims(`${e.target.naturalWidth}×${e.target.naturalHeight}`)} />
          </div>
          <p className="muted small">
            {dims ?? "…"} · {bytes(blob.size)} · {blob.type || "unknown type"}
            {extra ? ` · ${extra}` : ""}
          </p>
        </>
      ) : (
        <p className="empty">Nothing yet.</p>
      )}
    </Card>
  );
}

function Workbench() {
  const toast = useToast();
  const [params] = useSearchParams();
  const { apiKey, setApiKey } = useApiKey();
  const [source, setSource] = useState(params.get("object") ? "storage" : "upload");
  const [objects, setObjects] = useState([]);
  const [objectNameSel, setObjectNameSel] = useState(params.get("object") || "");
  const [input, setInput] = useState(null); // { blob, name }
  const [opts, setOpts] = useState({ width: 400, height: "", format: "webp" });
  const [result, setResult] = useState(null); // { blob, cpuMs }
  const [saveName, setSaveName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  // A key revoked while in use: drop it so the page asks for another.
  const fail = (err) => (err.status === 401 ? setApiKey(null) : setError(err));

  // Images stored in the key's project.
  useEffect(() => {
    gw.list(apiKey).then((list) => setObjects(list.filter((o) => o.content_type?.startsWith("image/"))), fail);
  }, [apiKey]);

  // Load the chosen stored image.
  useEffect(() => {
    if (source !== "storage" || !objectNameSel) return;
    gw.download(apiKey, objectNameSel).then((blob) => setInput({ blob, name: objectNameSel }), fail);
  }, [source, apiKey, objectNameSel]);

  // Suggest a name for saving the result.
  useEffect(() => {
    if (!input) return;
    const stem = objectName(input.name).replace(/\.[a-z0-9]+$/, "");
    const size = opts.width && opts.height ? `${opts.width}x${opts.height}` : opts.width ? `w${opts.width}` : opts.height ? `h${opts.height}` : "converted";
    const ext = EXT[opts.format] || input.name.split(".").pop();
    setSaveName(`${stem}-${size}.${ext}`);
  }, [input, opts]);

  const run = async () => {
    if (!input) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await gw.transform(apiKey, input.blob, opts));
    } catch (err) {
      setResult(null);
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    try {
      const name = objectName(saveName);
      await gw.upload(apiKey, name, result.blob);
      toast(`Saved ${name} to storage`, "info");
      setObjects((list) => (list.some((o) => o.name === name) ? list : [...list, { name, content_type: result.blob.type }]));
    } catch (err) {
      fail(err);
    }
  };

  const tooBig = input && input.blob.size > MAX_INPUT;
  const set = (k) => (e) => setOpts({ ...opts, [k]: e.target.value === "" ? "" : k === "format" ? e.target.value : Number(e.target.value) });
  const saving = result && input && result.blob.size < input.blob.size ? `${Math.round(100 - (100 * result.blob.size) / input.blob.size)}% smaller` : null;

  return (
    <>
      <ErrorNote error={error} />

      <Card title="1 · Choose an image">
        <div className="segmented">
          <button className={source === "upload" ? "" : "ghost"} onClick={() => setSource("upload")}>
            From my computer
          </button>
          <button className={source === "storage" ? "" : "ghost"} onClick={() => setSource("storage")}>
            From storage
          </button>
        </div>
        {source === "upload" ? (
          <input
            type="file"
            accept="image/png,image/jpeg,image/webp"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) {
                setInput({ blob: file, name: file.name });
                setResult(null);
              }
            }}
          />
        ) : objects.length === 0 ? (
          <p className="muted small">No images in this project yet. Upload some on the Files page.</p>
        ) : (
          <select
            value={objectNameSel}
            onChange={(e) => {
              setObjectNameSel(e.target.value);
              setResult(null);
            }}
          >
            <option value="">Pick a stored image…</option>
            {objects.map((o) => (
              <option key={o.name} value={o.name}>
                {o.name}
              </option>
            ))}
          </select>
        )}
        {tooBig && <p className="error">This image is {bytes(input.blob.size)}; the image service accepts up to 2 MB.</p>}
      </Card>

      <Card title="2 · Choose the output">
        <div className="segmented wrap">
          {PRESETS.map((p) => (
            <button key={p.label} className="ghost small-btn" onClick={() => setOpts({ width: p.width, height: p.height, format: p.format })}>
              {p.label}
            </button>
          ))}
        </div>
        <div className="inline">
          <label>
            Width (px)
            <input type="number" min="1" max="8000" value={opts.width} onChange={set("width")} placeholder="keep" />
          </label>
          <label>
            Height (px)
            <input type="number" min="1" max="8000" value={opts.height} onChange={set("height")} placeholder="keep" />
          </label>
          <label>
            Format
            <select value={opts.format} onChange={set("format")}>
              <option value="">same as input</option>
              <option value="webp">WebP</option>
              <option value="jpeg">JPEG</option>
              <option value="png">PNG</option>
            </select>
          </label>
        </div>
        <p className="muted small">Give only a width or only a height to keep the aspect ratio; both forces that exact size.</p>
        <button disabled={!input || busy || tooBig} onClick={run}>
          {busy ? "Processing…" : "Transform"}
        </button>
      </Card>

      <div className="grid two">
        <Preview title="Original" blob={input?.blob} />
        <Preview title="Result" blob={result?.blob} extra={result ? `${result.cpuMs} CPU-ms${saving ? ` · ${saving}` : ""}` : null} />
      </div>

      {result && (
        <Card title="3 · Keep the result">
          <div className="inline">
            <input value={saveName} onChange={(e) => setSaveName(e.target.value)} />
            <button onClick={save}>Save to storage</button>
            <button className="ghost" onClick={() => saveBlob(result.blob, objectName(saveName))}>
              Download
            </button>
          </div>
        </Card>
      )}
    </>
  );
}

export default function ImageTool() {
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Image tool</h1>
          <p className="muted">
            Resize and convert PNG, JPEG and WebP with the image service. Each job is metered by bytes processed and
            CPU time, like any API call.
          </p>
        </div>
      </div>
      <KeyGate purpose="Resize and convert images, from your computer or from storage.">
        <Workbench />
      </KeyGate>
    </div>
  );
}
