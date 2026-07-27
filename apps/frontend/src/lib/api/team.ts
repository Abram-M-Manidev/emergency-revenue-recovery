import { apiRequest } from "@/lib/api/client";
import type { AssignableRole, InviteTeamMemberPayload, TeamMember } from "@/lib/api/types";

const BASE = "/team/members";

export function fetchTeamMembers(): Promise<TeamMember[]> {
  return apiRequest<TeamMember[]>(BASE);
}

export function inviteTeamMember(payload: InviteTeamMemberPayload): Promise<TeamMember> {
  return apiRequest<TeamMember>(BASE, { method: "POST", body: payload });
}

export function setTeamMemberActive(memberId: string, isActive: boolean): Promise<TeamMember> {
  return apiRequest<TeamMember>(`${BASE}/${memberId}/status`, {
    method: "PATCH",
    body: { is_active: isActive },
  });
}

export function setTeamMemberRole(memberId: string, role: AssignableRole): Promise<TeamMember> {
  return apiRequest<TeamMember>(`${BASE}/${memberId}/role`, {
    method: "PATCH",
    body: { role },
  });
}
