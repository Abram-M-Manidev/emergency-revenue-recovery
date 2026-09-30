import { apiRequest } from "@/lib/api/client";
import type {
  ConfigurePagingPayload,
  EmergencyPage,
  PageAcknowledgement,
  PagingSettings,
} from "@/lib/api/types";

const SETTINGS = "/organizations/current/paging";

/** Null when this organization has never configured emergency paging. */
export function fetchPagingSettings(): Promise<PagingSettings | null> {
  return apiRequest<PagingSettings | null>(SETTINGS);
}

export function configurePagingSettings(payload: ConfigurePagingPayload): Promise<PagingSettings> {
  return apiRequest<PagingSettings>(SETTINGS, { method: "PUT", body: payload });
}

export function deletePagingSettings(): Promise<void> {
  return apiRequest<void>(SETTINGS, { method: "DELETE" });
}

/** The organization's most recent pages (recipient numbers masked). */
export function fetchRecentPages(limit = 50): Promise<EmergencyPage[]> {
  return apiRequest<EmergencyPage[]>(`/dispatch/pages?limit=${limit}`);
}

export function acknowledgeTicketPage(ticketId: string): Promise<PageAcknowledgement> {
  return apiRequest<PageAcknowledgement>(`/dispatch/tickets/${ticketId}/paging/acknowledge`, {
    method: "POST",
  });
}

/**
 * The public, session-less acknowledgement behind the link in a page's text
 * message. The signed token is the whole authority.
 */
export function acknowledgeWithLink(token: string): Promise<PageAcknowledgement> {
  return apiRequest<PageAcknowledgement>("/paging/acknowledge", {
    method: "POST",
    body: { token },
    skipAuthRetry: true,
  });
}
