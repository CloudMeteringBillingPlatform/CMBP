import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { ApiKeyProvider } from "./apiKey.jsx";
import { AuthProvider, isOperator, isOwner, useAuth } from "./auth.jsx";
import Layout from "./components/Layout.jsx";
import { Spinner, ToastProvider } from "./components/ui.jsx";
import { DeadLetters, SystemStatus } from "./pages/Admin.jsx";
import Alerts from "./pages/Alerts.jsx";
import { Login, Signup } from "./pages/Auth.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Files from "./pages/Files.jsx";
import ImageTool from "./pages/ImageTool.jsx";
import { InvoiceDetail, Invoices } from "./pages/Invoices.jsx";
import Plan from "./pages/Plan.jsx";
import Projects from "./pages/Projects.jsx";
import Team from "./pages/Team.jsx";
import Usage from "./pages/Usage.jsx";
import "./styles.css";

function Guard({ allow, children }) {
  const { me } = useAuth();
  if (me === undefined) return <Spinner />;
  if (me === null) return <Navigate to="/login" replace />;
  if (allow && !allow(me)) return <Navigate to="/" replace />;
  return children;
}

function PublicOnly({ children }) {
  const { me } = useAuth();
  if (me === undefined) return <Spinner />;
  return me ? <Navigate to="/" replace /> : children;
}

function App() {
  return (
    <Routes>
      <Route path="/login" element={<PublicOnly><Login /></PublicOnly>} />
      <Route path="/signup" element={<PublicOnly><Signup /></PublicOnly>} />
      <Route element={<Guard><Layout /></Guard>}>
        <Route index element={<Dashboard />} />
        <Route path="usage" element={<Usage />} />
        <Route path="files" element={<Files />} />
        <Route path="images" element={<ImageTool />} />
        <Route path="projects" element={<Guard allow={isOwner}><Projects /></Guard>} />
        <Route path="plan" element={<Guard allow={isOwner}><Plan /></Guard>} />
        <Route path="invoices" element={<Invoices />} />
        <Route path="invoices/:id" element={<InvoiceDetail />} />
        <Route path="alerts" element={<Alerts />} />
        <Route path="team" element={<Guard allow={isOwner}><Team /></Guard>} />
        <Route path="admin/dead-letters" element={<Guard allow={isOperator}><DeadLetters /></Guard>} />
        <Route path="admin/status" element={<Guard allow={isOperator}><SystemStatus /></Guard>} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <ApiKeyProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </ApiKeyProvider>
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
