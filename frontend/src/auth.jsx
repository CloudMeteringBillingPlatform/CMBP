import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "./api.js";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [me, setMe] = useState(undefined); // undefined = loading, null = signed out

  const refresh = useCallback(async () => {
    try {
      setMe(await api.get("/api/me"));
    } catch {
      setMe(null);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const login = async (email, password) => setMe(await api.post("/api/auth/login", { email, password }));
  const signup = async (body) => setMe(await api.post("/api/auth/signup", body));
  const logout = async () => {
    await api.post("/api/auth/logout");
    setMe(null);
  };

  return <AuthContext.Provider value={{ me, refresh, login, signup, logout }}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  return useContext(AuthContext);
}

// Role checks are enforced by the API; the UI mirrors them so people only see what they can do.
export const isOwner = (me) => me?.role === "owner";
export const isOperator = (me) => Boolean(me?.user?.is_operator);
