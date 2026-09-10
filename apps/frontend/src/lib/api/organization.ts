import { apiRequest } from "@/lib/api/client";
import type {
  ConfigureNotificationsPayload,
  NotificationSettings,
  Organization,
  UpdateOrganizationPayload,
} from "@/lib/api/types";

const BASE = "/organizations/current";
const NOTIFICATIONS = `${BASE}/notifications`;

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
