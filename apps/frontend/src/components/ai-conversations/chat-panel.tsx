"use client";

import { useEffect, useRef, useState } from "react";
import { Send } from "lucide-react";

import { FOLLOW_UP_REASON_LABEL } from "@/components/follow-ups/follow-up-queue";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  fetchConversationDetail,
  sendConversationMessage,
  startConversation,
} from "@/lib/api/ai-conversations";
import { ApiError } from "@/lib/api/client";
import { fetchVoiceCall } from "@/lib/api/voice";
import type {
  CallClassification,
  Conversation,
  ConversationMessage,
  ConversationOutcome,
  TransferReason,
  TransferStatus,
  VoiceCall,
} from "@/lib/api/types";
import { useToast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils/cn";

interface ChatPanelProps {
  conversationId: string | null;
  onConversationChange: (conversation: Conversation) => void;
}

/**
 * "Transfer accepted" is the strongest claim available: the provider moved
 * the call. Nothing confirms that anyone answered, so nothing here says so.
 */
const TRANSFER_STATUS_LABEL: Record<TransferStatus, string> = {
  requested: "Requested",
  destination_resolved: "Requested",
  initiated: "Transfer accepted",
  unavailable: "Not available",
  failed: "Failed",
};

const TRANSFER_STATUS_VARIANT: Record<TransferStatus, "success" | "destructive" | "secondary"> = {
  requested: "secondary",
  destination_resolved: "secondary",
  initiated: "success",
  unavailable: "secondary",
  failed: "destructive",
};

const TRANSFER_REASON_LABEL: Record<TransferReason, string> = {
  caller_requested: "Caller asked for a person",
  caller_frustrated: "Caller was frustrated",
  out_of_scope: "Outside what the assistant can do",
  emergency_policy: "Emergency handed to a person",
  system_unavailable: "Assistant was unavailable",
};

function classificationBadgeVariant(classification: CallClassification) {
  if (classification === "emergency") return "destructive";
  if (classification === "non_emergency") return "success";
  return "secondary";
}

export function ChatPanel({ conversationId, onConversationChange }: ChatPanelProps) {
  const { toast } = useToast();
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [outcome, setOutcome] = useState<ConversationOutcome | null>(null);
  const [voiceCall, setVoiceCall] = useState<VoiceCall | null>(null);
  const [isLoadingDetail, setIsLoadingDetail] = useState(false);
  const [isStarting, setIsStarting] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [draft, setDraft] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!conversationId) {
      setConversation(null);
      setMessages([]);
      setOutcome(null);
      setVoiceCall(null);
      return;
    }
    let cancelled = false;
    setIsLoadingDetail(true);
    fetchConversationDetail(conversationId)
      .then((detail) => {
        if (cancelled) return;
        setConversation(detail.conversation);
        setMessages(detail.messages);
        setOutcome(detail.outcome);
        if (detail.conversation.channel === "voice") {
          fetchVoiceCall(conversationId)
            .then((call) => {
              if (!cancelled) setVoiceCall(call);
            })
            .catch(() => {
              if (!cancelled) setVoiceCall(null);
            });
        } else {
          setVoiceCall(null);
        }
      })
      .catch(() => toast({ title: "Failed to load conversation", variant: "destructive" }))
      .finally(() => {
        if (!cancelled) setIsLoadingDetail(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function handleStart() {
    setIsStarting(true);
    try {
      const created = await startConversation();
      setConversation(created);
      setMessages([]);
      setOutcome(null);
      onConversationChange(created);
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to start conversation",
        variant: "destructive",
      });
    } finally {
      setIsStarting(false);
    }
  }

  async function handleSend() {
    if (!conversation || !draft.trim()) return;
    const text = draft.trim();
    setDraft("");
    setIsSending(true);
    setMessages((current) => [
      ...current,
      { id: `pending-${Date.now()}`, role: "customer", content: text, created_at: new Date().toISOString() },
    ]);
    try {
      const result = await sendConversationMessage(conversation.id, text);
      setMessages((current) => [...current, result.reply]);
      setOutcome(result.outcome);
      setConversation(result.conversation);
      onConversationChange(result.conversation);
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to send message",
        variant: "destructive",
      });
      setMessages((current) => current.filter((m) => !m.id.startsWith("pending-")));
      setDraft(text);
    } finally {
      setIsSending(false);
    }
  }

  const isCompleted = conversation?.status === "completed";

  return (
    <Card className="flex flex-col">
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="text-base">Talk to the AI Brain</CardTitle>
          <CardDescription>
            Send a message as if you were a caller — try an emergency and a routine question.
          </CardDescription>
        </div>
        <Button variant="outline" size="sm" onClick={handleStart} isLoading={isStarting}>
          New conversation
        </Button>
      </CardHeader>
      <CardContent className="flex-1 space-y-3">
        {outcome ? (
          <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted/30 p-3 text-sm">
            <Badge variant={classificationBadgeVariant(outcome.classification)}>
              {outcome.classification.replace("_", " ")}
            </Badge>
            <Badge variant="outline">{outcome.recommended_action.replace(/_/g, " ")}</Badge>
            <span className="text-muted-foreground">{outcome.summary}</span>
          </div>
        ) : null}

        {voiceCall ? (
          <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted/30 p-3 text-sm text-muted-foreground">
            <Badge variant="outline">Voice call</Badge>
            {voiceCall.caller_number ? <span>{voiceCall.caller_number}</span> : null}
            {voiceCall.duration_seconds != null ? (
              <span>{voiceCall.duration_seconds}s</span>
            ) : null}
            {voiceCall.ended_reason ? (
              <span>Ended: {voiceCall.ended_reason.replace(/-/g, " ")}</span>
            ) : null}
            {voiceCall.recording_url ? (
              <a
                href={voiceCall.recording_url}
                target="_blank"
                rel="noreferrer"
                className="text-primary underline"
              >
                Recording
              </a>
            ) : null}
            {voiceCall.recording_notice_missing ? (
              <Badge variant="destructive">No recording notice was given</Badge>
            ) : voiceCall.recording_url && voiceCall.disclosed_recording == null ? (
              <span className="text-xs">Recording notice: unknown</span>
            ) : null}
            {voiceCall.follow_up ? (
              <Badge variant={voiceCall.follow_up.resolved_at ? "secondary" : "destructive"}>
                {voiceCall.follow_up.resolved_at
                  ? "Called back"
                  : `Needs a callback: ${FOLLOW_UP_REASON_LABEL[voiceCall.follow_up.reason]}`}
              </Badge>
            ) : null}
          </div>
        ) : null}

        {voiceCall?.transfer_attempts && voiceCall.transfer_attempts.length > 0 ? (
          <div className="space-y-1 rounded-md border border-border bg-muted/30 p-3 text-sm">
            <p className="font-medium">Transfers to a person</p>
            {voiceCall.transfer_attempts.map((attempt, index) => (
              <div
                key={`${attempt.created_at ?? ""}-${index}`}
                className="flex flex-wrap items-center gap-2 text-muted-foreground"
              >
                <Badge variant={TRANSFER_STATUS_VARIANT[attempt.status]}>
                  {TRANSFER_STATUS_LABEL[attempt.status]}
                </Badge>
                <span>{TRANSFER_REASON_LABEL[attempt.reason]}</span>
                {attempt.destination ? (
                  <span>
                    to {attempt.destination_kind === "after_hours" ? "on-call" : "office"}{" "}
                    {attempt.destination}
                  </span>
                ) : null}
                {attempt.error_code ? (
                  <span className="text-xs">({attempt.error_code})</span>
                ) : null}
              </div>
            ))}
          </div>
        ) : null}

        {isLoadingDetail ? (
          <Skeleton className="h-64 w-full" />
        ) : !conversation ? (
          <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
            Start a new conversation or select one from the history to view it.
          </div>
        ) : (
          <div className="flex h-64 flex-col gap-2 overflow-y-auto rounded-md border border-border p-3">
            {messages.map((message) => (
              <div
                key={message.id}
                className={cn(
                  "max-w-[80%] rounded-lg px-3 py-2 text-sm",
                  message.role === "customer"
                    ? "self-end bg-primary text-primary-foreground"
                    : "self-start bg-muted text-foreground",
                )}
              >
                {message.content}
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        )}
      </CardContent>
      <CardFooter className="gap-2">
        <Input
          placeholder={
            !conversation
              ? "Start a conversation first…"
              : isCompleted
                ? "This conversation has ended."
                : "e.g. My furnace stopped working"
          }
          value={draft}
          disabled={!conversation || isCompleted || isSending}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") handleSend();
          }}
        />
        <Button
          onClick={handleSend}
          isLoading={isSending}
          disabled={!conversation || isCompleted || !draft.trim()}
        >
          <Send className="h-4 w-4" />
          Send
        </Button>
      </CardFooter>
    </Card>
  );
}
