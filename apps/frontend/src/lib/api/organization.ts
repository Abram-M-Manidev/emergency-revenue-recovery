import { apiRequest } from "@/lib/api/client";
import type { Organization, UpdateOrganizationPayload } from "@/lib/api/types";

const BASE = "/organizations/current";

export function fetchCurrentOrganization(): Promise<Organization> {
  return apiRequest<Organization>(BASE);
}

export function updateCurrentOrganization(
  payload: UpdateOrganizationPayload,
): Promise<Organization> {
  return apiRequest<Organization>(BASE, { method: "PATCH", body: payload });
}
