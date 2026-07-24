"use client";

import { useState } from "react";

import { ChatPanel } from "@/components/ai-conversations/chat-panel";
import { ConversationHistory } from "@/components/ai-conversations/conversation-history";
import type { Conversation } from "@/lib/api/types";

export default function AIConversationsPage() {
  const [selected, setSelected] = useState<Conversation | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  function handleConversationChange(conversation: Conversation) {
    setSelected(conversation);
    setRefreshKey((key) => key + 1);
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">AI Conversations</h1>
        <p className="text-sm text-muted-foreground">
          Test the AI Brain with a text conversation — grounded in your Business Knowledge, not
          hardcoded answers. Voice will front this same engine in a later milestone.
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        <ConversationHistory
          refreshKey={refreshKey}
          selectedId={selected?.id ?? null}
          onSelect={setSelected}
        />
        <ChatPanel conversationId={selected?.id ?? null} onConversationChange={handleConversationChange} />
      </div>
    </div>
  );
}
