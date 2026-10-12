import { useEffect, useState } from "react";
import { api } from "../api.js";
import { useAuth } from "../auth.jsx";
import { Badge, Card, ErrorNote, Spinner, useToast } from "../components/ui.jsx";
import { dateTime } from "../format.js";

export default function Team() {
  const { me, refresh } = useAuth();
  const toast = useToast();
  const [team, setTeam] = useState(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("viewer");
  const [link, setLink] = useState(null);
  const [error, setError] = useState(null);

  const load = () => api.get("/api/team").then(setTeam, setError);
  useEffect(() => {
    load();
  }, []);

  const invite = async (e) => {
    e.preventDefault();
    setError(null);
    try {
      const res = await api.post("/api/team/invites", { email, role });
      setLink(`${window.location.origin}${res.signup_path}`);
      setEmail("");
      load();
    } catch (err) {
      setError(err);
    }
  };

  const changeRole = async (member, newRole) => {
    setError(null);
    try {
      await api.patch(`/api/team/members/${member.user_id}`, { role: newRole });
      toast(`${member.email} is now ${newRole}`, "info");
      if (member.user_id === me.user.id) await refresh();
      load();
    } catch (err) {
      setError(err);
    }
  };

  if (!team) return error ? <ErrorNote error={error} /> : <Spinner />;
  return (
    <div className="page">
      <div className="page-head">
        <h1>Team</h1>
      </div>
      <ErrorNote error={error} />
      <Card title="Members">
        <table className="table">
          <thead>
            <tr>
              <th>Email</th>
              <th>Role</th>
              <th>Joined</th>
            </tr>
          </thead>
          <tbody>
            {team.members.map((m) => (
              <tr key={m.user_id}>
                <td>
                  {m.email} {m.user_id === me.user.id && <span className="muted small">(you)</span>}
                </td>
                <td>
                  <select value={m.role} onChange={(e) => changeRole(m, e.target.value)}>
                    <option value="owner">owner</option>
                    <option value="viewer">viewer</option>
                  </select>
                </td>
                <td className="muted">{dateTime(m.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <Card title="Invite someone">
        <form className="inline" onSubmit={invite}>
          <input type="email" placeholder="teammate@example.com" value={email} onChange={(e) => setEmail(e.target.value)} required />
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="viewer">viewer (read-only)</option>
            <option value="owner">owner</option>
          </select>
          <button>Create invite</button>
        </form>
        {link && (
          <div className="secret">
            <code>{link}</code>
            <button className="small-btn" onClick={() => navigator.clipboard?.writeText(link)}>
              Copy
            </button>
          </div>
        )}
        {link && <p className="muted small">Send this link; it works once, for that email address.</p>}
        {team.invites.length > 0 && (
          <table className="table compact">
            <thead>
              <tr>
                <th>Invited</th>
                <th>Role</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {team.invites.map((i) => (
                <tr key={i.id}>
                  <td>{i.email}</td>
                  <td>{i.role}</td>
                  <td>{i.accepted_at ? <Badge tone="ok">joined</Badge> : <Badge>pending</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
