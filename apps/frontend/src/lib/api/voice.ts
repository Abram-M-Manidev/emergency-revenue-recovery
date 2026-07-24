import { apiRequest } from "@/lib/api/client";
import type { VoiceCall, VoiceLine } from "@/lib/api/types";

const BASE = "/voice";

export function fetchVoiceLine(): Promise<VoiceLine | null> {
  return apiRequest<VoiceLine | null>(`${BASE}/line`);
}

export function fetchVoiceCall(conversationId: string): Promise<VoiceCall> {
  return apiRequest<VoiceCall>(`${BASE}/calls/${conversationId}`);
}
