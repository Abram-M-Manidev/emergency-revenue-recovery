export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

export const ROUTES = {
  home: "/",
  login: "/login",
  register: "/register",
  dashboard: "/dashboard",
  settings: "/settings",
  businessKnowledge: "/business-knowledge",
  aiConversations: "/ai-conversations",
} as const;

export const AUTH_COOKIE_NAME = "refresh_token";
