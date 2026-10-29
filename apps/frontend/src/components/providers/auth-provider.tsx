"use client";

import { createContext, useCallback, useEffect, useState, type ReactNode } from "react";

import * as authApi from "@/lib/api/auth";
import type { LoginPayload, RegisterPayload, UserProfile } from "@/lib/api/types";
import { ROUTES } from "@/lib/constants";

interface AuthContextValue {
  user: UserProfile | null;
  isLoading: boolean;
  login: (payload: LoginPayload) => Promise<void>;
  register: (payload: RegisterPayload) => Promise<void>;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserProfile | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function restoreSession() {
      // middleware.ts only lets session-less visitors render the public
      // landing page, so a refresh there could only 401.
      const session =
        window.location.pathname === ROUTES.home ? null : await authApi.refreshSession();
      if (cancelled) return;
      setUser(session?.user ?? null);
      setIsLoading(false);
    }

    void restoreSession();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (payload: LoginPayload) => {
    const response = await authApi.login(payload);
    setUser(response.user);
  }, []);

  const register = useCallback(async (payload: RegisterPayload) => {
    const response = await authApi.registerAccount(payload);
    setUser(response.user);
  }, []);

  const logout = useCallback(async () => {
    await authApi.logout();
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, isLoading, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}
