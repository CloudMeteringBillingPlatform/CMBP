import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth.jsx";
import { ErrorNote } from "../components/ui.jsx";

function AuthFrame({ title, subtitle, children }) {
  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="brand big">
          <span className="logo">☁</span> Mini Cloud
        </div>
        <h1>{title}</h1>
        {subtitle && <p className="muted">{subtitle}</p>}
        {children}
      </div>
    </div>
  );
}

export function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      navigate("/");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame title="Log in" subtitle="Metered object storage and image processing.">
      <form onSubmit={submit} className="stack">
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
        </label>
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        <ErrorNote error={error} />
        <button disabled={busy}>{busy ? "Logging in…" : "Log in"}</button>
      </form>
      <p className="muted small">
        New here? <Link to="/signup">Create an organization</Link>
      </p>
    </AuthFrame>
  );
}

export function Signup() {
  const { signup } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const invite = params.get("invite");
  const [form, setForm] = useState({ email: "", password: "", org_name: "" });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signup({ ...form, org_name: invite ? null : form.org_name, invite_token: invite || null });
      navigate("/");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame
      title={invite ? "Join your team" : "Create your organization"}
      subtitle={invite ? "Use the email address the invite was sent to." : "You will be its first owner."}
    >
      <form onSubmit={submit} className="stack">
        {!invite && (
          <label>
            Organization name
            <input value={form.org_name} onChange={set("org_name")} placeholder="Acme Inc." maxLength={100} />
          </label>
        )}
        <label>
          Email
          <input type="email" value={form.email} onChange={set("email")} required />
        </label>
        <label>
          Password <span className="muted small">(at least 8 characters)</span>
          <input type="password" value={form.password} onChange={set("password")} minLength={8} required />
        </label>
        <ErrorNote error={error} />
        <button disabled={busy}>{busy ? "Creating…" : invite ? "Join" : "Create organization"}</button>
      </form>
      <p className="muted small">
        Already have an account? <Link to="/login">Log in</Link>
      </p>
    </AuthFrame>
  );
}
