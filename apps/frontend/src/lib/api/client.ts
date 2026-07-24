import { API_BASE_URL } from "@/lib/constants";
import { getAccessToken, setAccessToken } from "@/lib/api/token-store";
import type { ApiErrorBody, AuthResponse } from "@/lib/api/types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: Record<string, unknown>;

  constructor(status: number, body: ApiErrorBody) {
    super(ApiError.deriveMessage(body));
    this.name = "ApiError";
    this.status = status;
    this.code = body.error.code;
    this.details = body.error.details;
  }

  /**
   * The API's top-level message for VALIDATION_ERROR is a generic
   * "One or more fields failed validation." — the actionable reason (e.g.
   * "At least one of postal_code or city is required.") lives in
   * `details.errors[].msg`. Surface that instead when present so toasts
   * built from `error.message` are useful rather than generic.
   */
  private static deriveMessage(body: ApiErrorBody): string {
    if (body.error.code === "VALIDATION_ERROR") {
      const errors = body.error.details?.errors;
      if (Array.isArray(errors) && errors.length > 0) {
        const messages = errors
          .map((entry) =>
            entry && typeof entry === "object" && typeof entry.msg === "string"
              ? entry.msg.replace(/^Value error,\s*/, "")
              : null,
          )
          .filter((message): message is string => Boolean(message));
        if (messages.length > 0) return messages.join(" ");
      }
    }
    return body.error.message;
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
