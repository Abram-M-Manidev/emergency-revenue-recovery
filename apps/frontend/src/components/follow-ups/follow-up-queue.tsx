"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { PhoneMissed } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/hooks/use-auth";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api/client";
import type { CallFollowUp, FollowUpReason } from "@/lib/api/types";
import { fetchFollowUps, resolveFollowUp } from "@/lib/api/voice";
import { ROUTES } from "@/lib/constants";

/**
 * What each reason honestly means. None of them says anyone was reached:
 * a follow-up exists precisely because nothing on the call confirmed it.
 */
export const FOLLOW_UP_REASON_LABEL: Record<FollowUpReason, string> = {
  ai_unavailable: "Assistant could not take the call",
  ended_before_request_recorded: "Hung up before details were taken",
  transferred_before_request_recorded: "Transferred — confirm someone answered",
};

function formatWhen(value: string | null): string {
  if (!value) return "";
  return new Date(value).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Calls that ended without their request being recorded, waiting for a callback. */
export function FollowUpQueue() {
  const { user } = useAuth();
  const { toast } = useToast();
  const [items, setItems] = useState<CallFollowUp[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [resolvingId, setResolvingId] = useState<string | null>(null);
  const canResolve = user?.permissions.includes("dispatch:manage") ?? false;

  const load = useCallback(() => {
    fetchFollowUps()
      .then((data) => {
        setItems(data);
        setFailed(false);
      })
      .catch(() => setFailed(true));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleResolve(followUp: CallFollowUp) {
    setResolvingId(followUp.id);
    try {
      await resolveFollowUp(followUp.id);
      setItems((current) => (current ?? []).filter((item) => item.id !== followUp.id));
      toast({ title: "Marked as called back", variant: "success" });
    } catch (error) {
      toast({
        title: "Could not update the follow-up",
        description: error instanceof ApiError ? error.message : undefined,
        variant: "destructive",
      });
    } finally {
      setResolvingId(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Calls needing a callback</CardTitle>
        <CardDescription>
          Calls that ended before the caller&apos;s request was recorded. Possible emergencies
          are listed first and also appear in Dispatch.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {failed ? (
          <p className="text-sm text-muted-foreground">Follow-ups could not be loaded.</p>
        ) : items === null ? (
          <Skeleton className="h-20 w-full" />
        ) : items.length === 0 ? (
          <EmptyState
            icon={PhoneMissed}
            title="Nothing waiting"
            description="Every recent call either recorded its request or was answered in full."
          />
        ) : (
          <ul className="divide-y divide-border">
            {items.map((item) => (
              <li
                key={item.id}
                className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm">
                  {item.possible_emergency ? (
                    <Badge variant="destructive">Possible emergency</Badge>
                  ) : null}
                  <span className="font-medium">{item.caller_number ?? "Unknown number"}</span>
                  <span className="text-muted-foreground">
                    {FOLLOW_UP_REASON_LABEL[item.reason]}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {formatWhen(item.created_at)}
                  </span>
                  {item.emergency_ticket_id ? (
                    <Link href={ROUTES.dispatch} className="text-xs text-primary underline">
                      Ticket in Dispatch
                    </Link>
                  ) : null}
                </div>
                {canResolve ? (
                  <Button
                    variant="outline"
                    size="sm"
                    isLoading={resolvingId === item.id}
                    onClick={() => handleResolve(item)}
                  >
                    Mark called back
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
