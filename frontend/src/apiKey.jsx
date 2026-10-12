import { createContext, useContext, useState } from "react";
import { gw } from "./api.js";
import { Badge, Card, ErrorNote } from "./components/ui.jsx";

// The API key the Files and Image tool pages use. It lives in memory and in sessionStorage (so
// it survives a reload but is gone when the tab closes); never in localStorage.
const STORAGE_KEY = "console.apiKey";
const KeyContext = createContext(null);

function readStored() {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStored(key) {
  try {
    if (key) window.sessionStorage.setItem(STORAGE_KEY, key);
    else window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* private mode: memory only */
  }
}

export function ApiKeyProvider({ children }) {
  const [apiKey, setApiKeyState] = useState(readStored);
  const setApiKey = (key) => {
    writeStored(key);
    setApiKeyState(key);
  };
  return <KeyContext.Provider value={{ apiKey, setApiKey }}>{children}</KeyContext.Provider>;
}

export function useApiKey() {
  return useContext(KeyContext);
}

export const keyPrefix = (key) => (key ? key.split("_").slice(0, 2).join("_") : "");

// Shows a "connect" form until a working key is set, then the page itself with a small key bar.
export function KeyGate({ purpose, children }) {
  const { apiKey, setApiKey } = useApiKey();
  const [draft, setDraft] = useState("");
  const [error, setError] = useState(null);
  const [checking, setChecking] = useState(false);

  const connect = async (e) => {
    e.preventDefault();
    const key = draft.trim();
    setChecking(true);
    setError(null);
    try {
      await gw.list(key); // the cheapest call that proves the key works
      setApiKey(key);
      setDraft("");
    } catch (err) {
      setError(err.status === 401 ? "That key was not accepted: it is unknown or revoked." : err.message);
    } finally {
      setChecking(false);
    }
  };

  if (!apiKey) {
    return (
      <Card title="Connect with an API key">
        <p className="muted">
          {purpose} The console calls the public gateway exactly like any API client, so it needs one of your
          project's API keys. Each key belongs to one project; use that project's key to work with its files.
        </p>
        <form className="inline" onSubmit={connect}>
          <input
            type="password"
            placeholder="mk_xxxxxxxx_…"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            autoComplete="off"
            required
          />
          <button disabled={checking}>{checking ? "Checking…" : "Connect"}</button>
        </form>
        <ErrorNote error={error} />
        <p className="muted small">
          Lost your key? Owners can create a new one in Projects &amp; keys (keys are shown only once). The key stays in
          this browser tab only and is forgotten when you close it.
        </p>
      </Card>
    );
  }

  return (
    <>
      <div className="keybar">
        <span>
          Using API key <code>{keyPrefix(apiKey)}…</code> <Badge tone="ok">connected</Badge>
        </span>
        <button className="ghost small-btn" onClick={() => setApiKey(null)}>
          Use a different key
        </button>
      </div>
      {children}
    </>
  );
}
