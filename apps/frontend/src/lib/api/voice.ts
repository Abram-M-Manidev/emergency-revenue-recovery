import { apiRequest } from "@/lib/api/client";
import type { CallFollowUp, VoiceCall, VoiceLine } from "@/lib/api/types";

const BASE = "/voice";

export function fetchVoiceLine(): Promise<VoiceLine | null> {
  return apiRequest<VoiceLine | null>(`${BASE}/line`);
}

export function fetchVoiceCall(conversationId: string): Promise<VoiceCall> {
  return apiRequest<VoiceCall>(`${BASE}/calls/${conversationId}`);
}

/** Calls that ended without their request being recorded; open ones unless asked. */
export function fetchFollowUps(includeResolved = false): Promise<CallFollowUp[]> {
  return apiRequest<CallFollowUp[]>(
    `${BASE}/follow-ups${includeResolved ? "?include_resolved=true" : ""}`,
  );
}

export function resolveFollowUp(followUpId: string): Promise<CallFollowUp> {
  return apiRequest<CallFollowUp>(`${BASE}/follow-ups/${followUpId}/resolve`, { method: "POST" });
}
