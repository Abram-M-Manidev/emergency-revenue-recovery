import { apiRequest } from "@/lib/api/client";
import type {
  CallDisclosureSettings,
  CallTransferSettings,
  ConfigureCallDisclosurePayload,
  ConfigureCallTransferPayload,
  ConfigureNotificationsPayload,
  NotificationSettings,
  Organization,
  UpdateOrganizationPayload,
} from "@/lib/api/types";

const BASE = "/organizations/current";
const NOTIFICATIONS = `${BASE}/notifications`;
const CALL_TRANSFER = `${BASE}/call-transfer`;
const DISCLOSURE = `${BASE}/disclosure`;

export function fetchCurrentOrganization(): Promise<Organization> {
  return apiRequest<Organization>(BASE);
}

export function updateCurrentOrganization(
  payload: UpdateOrganizationPayload,
): Promise<Organization> {
  return apiRequest<Organization>(BASE, { method: "PATCH", body: payload });
}

/** Null when this organization has never configured emergency alerting. */
export function fetchNotificationSettings(): Promise<NotificationSettings | null> {
  return apiRequest<NotificationSettings | null>(NOTIFICATIONS);
}

export function configureNotificationSettings(
  payload: ConfigureNotificationsPayload,
): Promise<NotificationSettings> {
  return apiRequest<NotificationSettings>(NOTIFICATIONS, { method: "PUT", body: payload });
}

/**
 * Pause or resume alerting without re-submitting the webhook URL — every
 * extra time a credential is typed or transmitted is another chance to leak
 * it.
 */
export function setNotificationsEnabled(isEnabled: boolean): Promise<NotificationSettings> {
  return apiRequest<NotificationSettings>(NOTIFICATIONS, {
    method: "PATCH",
    body: { is_enabled: isEnabled },
  });
}

export function deleteNotificationSettings(): Promise<void> {
  return apiRequest<void>(NOTIFICATIONS, { method: "DELETE" });
}

/** Null when this organization has never configured human transfer. */
export function fetchCallTransferSettings(): Promise<CallTransferSettings | null> {
  return apiRequest<CallTransferSettings | null>(CALL_TRANSFER);
}

export function configureCallTransferSettings(
  payload: ConfigureCallTransferPayload,
): Promise<CallTransferSettings> {
  return apiRequest<CallTransferSettings>(CALL_TRANSFER, { method: "PUT", body: payload });
}

export function deleteCallTransferSettings(): Promise<void> {
  return apiRequest<void>(CALL_TRANSFER, { method: "DELETE" });
}

/** Always a policy: with nothing saved, the default (both notices) applies. */
export function fetchCallDisclosureSettings(): Promise<CallDisclosureSettings> {
  return apiRequest<CallDisclosureSettings>(DISCLOSURE);
}

export function configureCallDisclosureSettings(
  payload: ConfigureCallDisclosurePayload,
): Promise<CallDisclosureSettings> {
  return apiRequest<CallDisclosureSettings>(DISCLOSURE, { method: "PUT", body: payload });
}

export function resetCallDisclosureSettings(): Promise<void> {
  return apiRequest<void>(DISCLOSURE, { method: "DELETE" });
}
