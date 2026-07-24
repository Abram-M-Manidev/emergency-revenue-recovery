"use client";

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fetchConversations } from "@/lib/api/ai-conversations";
import type { Conversation } from "@/lib/api/types";
import { cn } from "@/lib/utils/cn";

interface ConversationHistoryProps {
  refreshKey: number;
  selectedId: string | null;
  onSelect: (conversation: Conversation) => void;
}

function statusBadgeVariant(status: Conversation["status"]) {
  return status === "completed" ? "secondary" : "outline";
}

function channelLabel(channel: Conversation["channel"]) {
  return channel === "voice" ? "Voice" : "Text";
}

export function ConversationHistory({ refreshKey, selectedId, onSelect }: ConversationHistoryProps) {
  const [isLoading, setIsLoading] = useState(true);
  const [conversations, setConversations] = useState<Conversation[]>([]);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    fetchConversations()
      .then((data) => {
        if (!cancelled) setConversations(data);
      })
      .catch(() => {
        /* surfaced via the chat panel instead of a second toast here */
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Past conversations</CardTitle>
        <CardDescription>Every conversation the AI Brain has handled for this org, text or voice.</CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : conversations.length === 0 ? (
          <EmptyState
            title="No conversations yet"
            description="Start one below to see how the AI Brain classifies and responds."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Started</TableHead>
                <TableHead>Channel</TableHead>
                <TableHead>Status</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {conversations.map((conversation) => (
                <TableRow
                  key={conversation.id}
                  onClick={() => onSelect(conversation)}
                  className={cn(
                    "cursor-pointer",
                    conversation.id === selectedId && "bg-muted/50",
                  )}
                >
                  <TableCell>{new Date(conversation.started_at).toLocaleString()}</TableCell>
                  <TableCell>
                    <Badge variant={conversation.channel === "voice" ? "default" : "outline"}>
                      {channelLabel(conversation.channel)}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant={statusBadgeVariant(conversation.status)}>
                      {conversation.status}
                    </Badge>
                  </TableCell>
                  <TableCell />
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
