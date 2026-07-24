/**
 * The access token lives in memory only — never localStorage/sessionStorage
 * — so an XSS payload can't read it off disk. It's lost on full page reload
 * by design; the client silently re-derives it from the httpOnly refresh
 * cookie via POST /auth/refresh (see client.ts).
 */

type Listener = (token: string | null) => void;

let currentToken: string | null = null;
const listeners = new Set<Listener>();

export function getAccessToken(): string | null {
  return currentToken;
}

export function setAccessToken(token: string | null): void {
  currentToken = token;
  listeners.forEach((listener) => listener(token));
}

export function subscribeToAccessToken(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
