"use client";

/**
 * Where a live call is handed to a person — the caller's human exit.
 *
 * During business hours calls go to the office number; after hours to the
 * on-call number. When neither applies, the assistant tells the caller
 * honestly that it can't connect them right now and offers a callback — it
 * never pretends to transfer.
 *
 * Neither number may be this business's own AI line: that would send the
 * caller straight back to the assistant. The API refuses it.
 */

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api/client";
import {
  configureCallTransferSettings,
  deleteCallTransferSettings,
  fetchCallTransferSettings,
} from "@/lib/api/organization";
import type { CallTransferSettings } from "@/lib/api/types";

interface Props {
  canManage: boolean;
}

export function HumanTransferCard({ canManage }: Props) {
  const { toast } = useToast();
  const [settings, setSettings] = useState<CallTransferSettings | null>(null);
  const [isLoading, setIsLoading] = useState(canManage);
  const [office, setOffice] = useState("");
  const [onCall, setOnCall] = useState("");
  const [transferEmergencies, setTransferEmergencies] = useState(false);
  const [isEnabled, setIsEnabled] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isRemoving, setIsRemoving] = useState(false);

  function load(loaded: CallTransferSettings | null) {
    setSettings(loaded);
    setOffice(loaded?.business_hours_number ?? "");
    setOnCall(loaded?.after_hours_number ?? "");
    setTransferEmergencies(loaded?.transfer_emergencies ?? false);
    setIsEnabled(loaded?.is_enabled ?? true);
  }

  useEffect(() => {
    if (!canManage) return;
    let cancelled = false;
    fetchCallTransferSettings()
      .then((loaded) => {
        if (!cancelled) load(loaded);
      })
      .catch(() => toast({ title: "Failed to load human transfer settings", variant: "destructive" }))
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canManage]);

  async function save() {
    setIsSaving(true);
    try {
      const saved = await configureCallTransferSettings({
        business_hours_number: office.trim() || null,
        after_hours_number: onCall.trim() || null,
        transfer_emergencies: transferEmergencies,
        is_enabled: isEnabled,
      });
      load(saved);
      toast({ title: "Human transfer saved", variant: "success" });
    } catch (error) {
      toast({
        // The 422 names the rule broken (not E.164, or the AI line itself).
        title: error instanceof ApiError ? error.message : "Failed to save human transfer",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  }

  async function remove() {
    setIsRemoving(true);
    try {
      await deleteCallTransferSettings();
      load(null);
      toast({ title: "Human transfer removed", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to remove human transfer",
        variant: "destructive",
      });
    } finally {
      setIsRemoving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Human transfer</CardTitle>
        <CardDescription>
          Where a caller is connected when they ask for a person, are upset, or need something the
          assistant can&apos;t handle. Without a number here, callers are told honestly that nobody
          can be connected right now and are offered a callback.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!canManage ? (
          <EmptyState
            title="Owner-only settings"
            description="Ask an Owner to configure where calls are transferred."
          />
        ) : isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <div className="space-y-4">
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">Status</span>
              <Badge variant={settings?.is_enabled ? "success" : "secondary"}>
                {!settings ? "Not configured" : settings.is_enabled ? "Active" : "Off"}
              </Badge>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium" htmlFor="transfer-office">
                Office number (business hours)
              </label>
              <Input
                id="transfer-office"
                type="tel"
                placeholder="+15551234567"
                value={office}
                onChange={(event) => setOffice(event.target.value)}
                autoComplete="off"
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium" htmlFor="transfer-on-call">
                On-call number (after hours)
              </label>
              <Input
                id="transfer-on-call"
                type="tel"
                placeholder="+15557654321"
                value={onCall}
                onChange={(event) => setOnCall(event.target.value)}
                autoComplete="off"
              />
              <p className="text-xs text-muted-foreground">
                Full international format (e.g. +15551234567). If there is no office number, calls
                in business hours go to the on-call number; after hours with no on-call number,
                nobody is rung — the closed office is never used as a fallback.
              </p>
            </div>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="mt-1"
                checked={transferEmergencies}
                onChange={(event) => setTransferEmergencies(event.target.checked)}
              />
              <span>
                Connect emergency callers to a person once their emergency is recorded
                <span className="block text-xs text-muted-foreground">
                  The emergency ticket and alert are always created first; this never replaces them.
                </span>
              </span>
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={isEnabled}
                onChange={(event) => setIsEnabled(event.target.checked)}
              />
              Human transfer enabled
            </label>
            <div className="flex gap-2 border-t border-border pt-4">
              <Button onClick={save} isLoading={isSaving}>
                Save
              </Button>
              {settings ? (
                <Button variant="destructive" size="sm" onClick={remove} isLoading={isRemoving}>
                  Remove
                </Button>
              ) : null}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
