import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useApiKey } from "../apiKey.jsx";
import { isOperator, isOwner, useAuth } from "../auth.jsx";
import { Badge } from "./ui.jsx";

export default function Layout() {
  const { me, logout } = useAuth();
  const { setApiKey } = useApiKey();
  const navigate = useNavigate();
  const owner = isOwner(me);
  const operator = isOperator(me);

  const links = [
    { to: "/", label: "Dashboard", show: true, end: true },
    { to: "/usage", label: "Usage explorer", show: true },
    { to: "/files", label: "Files", show: true },
    { to: "/images", label: "Image tool", show: true },
    { to: "/projects", label: "Projects & keys", show: owner },
    { to: "/plan", label: "Plan", show: owner },
    { to: "/invoices", label: "Invoices", show: true },
    { to: "/alerts", label: "Alerts", show: true },
    { to: "/team", label: "Team", show: owner },
  ];
  const opLinks = [
    { to: "/admin/dead-letters", label: "Dead letters" },
    { to: "/admin/status", label: "System status" },
  ];

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="logo">☁</span> Mini Cloud
        </div>
        <div className="org">
          <div className="org-name">{me.org.name}</div>
          <div className="org-user">
            {me.user.email} <Badge tone={owner ? "info" : "neutral"}>{me.role}</Badge>
          </div>
        </div>
        <nav>
          {links
            .filter((l) => l.show)
            .map((l) => (
              <NavLink key={l.to} to={l.to} end={l.end}>
                {l.label}
              </NavLink>
            ))}
          {operator && (
            <>
              <div className="nav-section">Operator</div>
              {opLinks.map((l) => (
                <NavLink key={l.to} to={l.to}>
                  {l.label}
                </NavLink>
              ))}
            </>
          )}
        </nav>
        <a className="docs-link" href="/docs" target="_blank" rel="noreferrer">
          API reference ↗
        </a>
        <button
          className="ghost logout"
          onClick={async () => {
            setApiKey(null); // a pasted API key never outlives the session
            await logout();
            navigate("/login");
          }}
        >
          Log out
        </button>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
