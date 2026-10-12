import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api.js";
import { useApiKey } from "../apiKey.jsx";
import { Badge, Card, Empty, ErrorNote, Spinner, useToast } from "../components/ui.jsx";
import { bytes, dateTime } from "../format.js";

function NewKey({ created, onClose }) {
  const [copied, setCopied] = useState(false);
  const { setApiKey } = useApiKey();
  const navigate = useNavigate();
  const origin = window.location.origin;
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Your new API key</h2>
        <p className="warn-text">Copy it now. It is shown once and only its hash is stored.</p>
        <div className="secret">
          <code>{created.key}</code>
          <button
            className="small-btn"
            onClick={async () => {
              await navigator.clipboard?.writeText(created.key);
              setCopied(true);
            }}
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
        <p className="muted small">Try it:</p>
        <pre className="code">{`curl -X PUT ${origin}/v1/storage/objects/hello.txt \\
  -H "Authorization: Bearer ${created.key}" --data-binary @hello.txt

curl ${origin}/v1/storage/objects -H "Authorization: Bearer ${created.key}"

curl -X POST "${origin}/v1/images/transform?width=200&format=webp" \\
  -H "Authorization: Bearer ${created.key}" --data-binary @photo.png -o out.webp`}</pre>
        <div className="inline">
          <button
            className="ghost"
            onClick={() => {
              setApiKey(created.key);
              navigate("/files");
            }}
          >
            Use this key in the console
          </button>
          <button onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  );
}

function Keys({ project }) {
  const toast = useToast();
  const [keys, setKeys] = useState(null);
  const [name, setName] = useState("");
  const [created, setCreated] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      setKeys((await api.get(`/api/projects/${project.id}/keys`)).keys);
    } catch (err) {
      setError(err);
    }
  }, [project.id]);

  useEffect(() => {
    load();
  }, [load]);

  const create = async (e) => {
    e.preventDefault();
    setError(null);
    try {
      setCreated(await api.post(`/api/projects/${project.id}/keys`, { name }));
      setName("");
      load();
    } catch (err) {
      setError(err);
    }
  };

  const revoke = async (key) => {
    if (!window.confirm(`Revoke ${key.prefix}…? Requests using it will be rejected.`)) return;
    await api.del(`/api/keys/${key.id}`);
    toast(`Key ${key.prefix}… revoked`, "info");
    load();
  };

  return (
    <Card title={`API keys · ${project.name}`}>
      <form className="inline" onSubmit={create}>
        <input placeholder="Key name (optional)" value={name} onChange={(e) => setName(e.target.value)} maxLength={64} />
        <button>Create key</button>
      </form>
      <ErrorNote error={error} />
      {!keys ? (
        <Spinner />
      ) : keys.length === 0 ? (
        <Empty>No keys yet. Create one to call the gateway.</Empty>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Prefix</th>
              <th>Name</th>
              <th>Created</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {keys.map((k) => (
              <tr key={k.id}>
                <td>
                  <code>{k.prefix}…</code>
                </td>
                <td>{k.name || <span className="muted">—</span>}</td>
                <td className="muted">{dateTime(k.created_at)}</td>
                <td>
                  <Badge tone={k.status === "active" ? "ok" : "neutral"}>{k.status}</Badge>
                </td>
                <td className="num">
                  {k.status === "active" && (
                    <button className="danger small-btn" onClick={() => revoke(k)}>
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {created && <NewKey created={created} onClose={() => setCreated(null)} />}
    </Card>
  );
}

export default function Projects() {
  const [projects, setProjects] = useState(null);
  const [selected, setSelected] = useState(null);
  const [name, setName] = useState("");
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    const list = (await api.get("/api/projects")).projects;
    setProjects(list);
    setSelected((s) => list.find((p) => p.id === s?.id) || list[0] || null);
  }, []);

  useEffect(() => {
    load().catch(setError);
  }, [load]);

  const create = async (e) => {
    e.preventDefault();
    setError(null);
    try {
      const p = await api.post("/api/projects", { name });
      setName("");
      await load();
      setSelected(p);
    } catch (err) {
      setError(err);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1>Projects & keys</h1>
      </div>
      <div className="grid two-one">
        <Card title="Projects">
          <form className="inline" onSubmit={create}>
            <input placeholder="New project name" value={name} onChange={(e) => setName(e.target.value)} required />
            <button>Create</button>
          </form>
          <ErrorNote error={error} />
          {!projects ? (
            <Spinner />
          ) : projects.length === 0 ? (
            <Empty>Create a project, then an API key for it.</Empty>
          ) : (
            <ul className="pick-list">
              {projects.map((p) => (
                <li key={p.id} className={selected?.id === p.id ? "active" : ""} onClick={() => setSelected(p)}>
                  <div className="strong">{p.name}</div>
                  <div className="muted small">
                    {p.active_keys} active key(s) · {bytes(p.stored_bytes)} stored
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
        {selected ? <Keys key={selected.id} project={selected} /> : <div />}
      </div>
    </div>
  );
}
