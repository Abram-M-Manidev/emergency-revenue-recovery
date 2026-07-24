import { apiRequest } from "@/lib/api/client";
import type { Conversation, ConversationDetail, SendMessageResult } from "@/lib/api/types";

const BASE = "/ai/conversations";

export function startConversation(callerPhoneNumber?: string): Promise<Conversation> {
  return apiRequest<Conversation>(BASE, {
    method: "POST",
    body: { caller_phone_number: callerPhoneNumber ?? null },
  });
}

export function fetchConversations(): Promise<Conversation[]> {
  return apiRequest<Conversation[]>(BASE);
}

export function fetchConversationDetail(conversationId: string): Promise<ConversationDetail> {
  return apiRequest<ConversationDetail>(`${BASE}/${conversationId}`);
}

export function sendConversationMessage(
  conversationId: string,
  message: string,
): Promise<SendMessageResult> {
  return apiRequest<SendMessageResult>(`${BASE}/${conversationId}/messages`, {
    method: "POST",
    body: { message },
  });
}
