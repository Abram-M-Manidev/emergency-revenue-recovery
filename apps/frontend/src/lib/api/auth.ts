import { apiRequest } from "@/lib/api/client";
import { setAccessToken } from "@/lib/api/token-store";
import type { AuthResponse, LoginPayload, RegisterPayload, UserProfile } from "@/lib/api/types";

export async function registerAccount(payload: RegisterPayload): Promise<AuthResponse> {
  const response = await apiRequest<AuthResponse>("/auth/register", {
    method: "POST",
    body: payload,
    skipAuthRetry: true,
  });
  setAccessToken(response.tokens.access_token);
  return response;
}

export async function login(payload: LoginPayload): Promise<AuthResponse> {
  const response = await apiRequest<AuthResponse>("/auth/login", {
    method: "POST",
    body: payload,
    skipAuthRetry: true,
  });
  setAccessToken(response.tokens.access_token);
  return response;
}

export async function logout(): Promise<void> {
  try {
    await apiRequest<void>("/auth/logout", { method: "POST", skipAuthRetry: true });
  } finally {
    setAccessToken(null);
  }
}

export async function fetchCurrentUser(): Promise<UserProfile> {
  return apiRequest<UserProfile>("/auth/me");
}

export async function refreshSession(): Promise<AuthResponse | null> {
  try {
    const response = await apiRequest<AuthResponse>("/auth/refresh", {
      method: "POST",
      skipAuthRetry: true,
    });
    setAccessToken(response.tokens.access_token);
    return response;
  } catch {
    setAccessToken(null);
    return null;
  }
}
