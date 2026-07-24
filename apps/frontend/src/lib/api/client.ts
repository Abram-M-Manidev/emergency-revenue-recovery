import { API_BASE_URL } from "@/lib/constants";
import { getAccessToken, setAccessToken } from "@/lib/api/token-store";
import type { ApiErrorBody, AuthResponse } from "@/lib/api/types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: Record<string, unknown>;

  constructor(status: number, body: ApiErrorBody) {
    super(body.error.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.error.code;
    this.details = body.error.details;
  }
}

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  /** Skip the automatic refresh-and-retry-on-401 behavior (used by the refresh call itself). */
  skipAuthRetry?: boolean;
}

let refreshInFlight: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const response = await fetch(`${API_BASE_URL}/auth/refresh`, {
          method: "POST",
          credentials: "include",
        });
        if (!response.ok) {
          setAccessToken(null);
          return false;
        }
        const body = (await response.json()) as AuthResponse;
        setAccessToken(body.tokens.access_token);
        return true;
      } catch {
        setAccessToken(null);
        return false;
      } finally {
        refreshInFlight = null;
      }
    })();
  }
  return refreshInFlight;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, skipAuthRetry, headers, ...rest } = options;

  const doFetch = async (): Promise<Response> => {
    const requestHeaders = new Headers(headers);
    requestHeaders.set("Accept", "application/json");
    const token = getAccessToken();
    if (token) {
      requestHeaders.set("Authorization", `Bearer ${token}`);
    }

    let requestBody: BodyInit | undefined;
    if (body !== undefined) {
      requestHeaders.set("Content-Type", "application/json");
      requestBody = JSON.stringify(body);
    }

    return fetch(`${API_BASE_URL}${path}`, {
      ...rest,
      headers: requestHeaders,
      body: requestBody,
      credentials: "include",
    });
  };

  let response = await doFetch();

  if (response.status === 401 && !skipAuthRetry) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      response = await doFetch();
    }
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json") ? await response.json() : undefined;

  if (!response.ok) {
    throw new ApiError(
      response.status,
      (payload as ApiErrorBody) ?? {
        error: { code: "UNKNOWN_ERROR", message: response.statusText },
        request_id: null,
      },
    );
  }

  return payload as T;
}
