"use client";

/**
 * Who is paged about an emergency, how, and how long they have to
 * acknowledge before the backup is paged.
 *
 * A page that was SENT is not a page that was ACKNOWLEDGED: escalation stops
 * only when a person presses Acknowledge (the link in the text message, or
 * the dispatch dashboard). If nobody does, the emergency is marked unresolved.
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
  configurePagingSettings,
  deletePagingSettings,
  fetchPagingSettings,
} from "@/lib/api/paging";
import type { PagingSettings } from "@/lib/api/types";

interface Props {
  canManage: boolean;
}

const DEFAULT_TIMEOUT_MINUTES = 5;

export function EmergencyPagingCard({ canManage }: Props) {
  const { toast } = useToast();
  const [settings, setSettings] = useState<PagingSettings | null>(null);
  const [isLoading, setIsLoading] = useState(canManage);
  const [primary, setPrimary] = useState("");
  const [backup, setBackup] = useState("");
  const [smsEnabled, setSmsEnabled] = useState(true);
  const [voiceEnabled, setVoiceEnabled] = useState(false);
  const [timeoutMinutes, setTimeoutMinutes] = useState(String(DEFAULT_TIMEOUT_MINUTES));
  const [isEnabled, setIsEnabled] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isRemoving, setIsRemoving] = useState(false);

  function load(loaded: PagingSettings | null) {
    setSettings(loaded);
    setPrimary(loaded?.primary_number ?? "");
    setBackup(loaded?.backup_number ?? "");
    setSmsEnabled(loaded?.sms_enabled ?? true);
    setVoiceEnabled(loaded?.voice_enabled ?? false);
    setTimeoutMinutes(
      String(loaded ? Math.round(loaded.ack_timeout_seconds / 60) : DEFAULT_TIMEOUT_MINUTES),
    );
    setIsEnabled(loaded?.is_enabled ?? true);
  }

  useEffect(() => {
    if (!canManage) return;
    let cancelled = false;
    fetchPagingSettings()
      .then((loaded) => {
        if (!cancelled) load(loaded);
      })
      .catch(() => toast({ title: "Failed to load emergency paging settings", variant: "destructive" }))
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
      const saved = await configurePagingSettings({
        is_enabled: isEnabled,
        primary_number: primary.trim() || null,
        backup_number: backup.trim() || null,
        sms_enabled: smsEnabled,
        voice_enabled: voiceEnabled,
        ack_timeout_seconds: Math.round(Number(timeoutMinutes) * 60),
      });
      load(saved);
      toast({ title: "Emergency paging saved", variant: "success" });
    } catch (error) {
      toast({
        // The 422 names the rule broken; it never echoes the number back.
        title: error instanceof ApiError ? error.message : "Failed to save emergency paging",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  }

  async function remove() {
    setIsRemoving(true);
    try {
      await deletePagingSettings();
      load(null);
      toast({ title: "Emergency paging removed", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to remove emergency paging",
        variant: "destructive",
      });
    } finally {
      setIsRemoving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Emergency paging</CardTitle>
        <CardDescription>
          Who is paged by text message or automated call when an emergency is recorded. If the
          primary does not acknowledge in time, the backup is paged; if nobody acknowledges, the
          emergency is marked unresolved on the Dispatch page. A delivered page is not an
          acknowledgement — only pressing Acknowledge is.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!canManage ? (
          <EmptyState
            title="Owner-only settings"
            description="Ask an Owner to configure who is paged about emergencies."
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
              <label className="text-sm font-medium" htmlFor="paging-primary">
                Primary on-call number
              </label>
              <Input
                id="paging-primary"
                type="tel"
                placeholder="+15551234567"
                value={primary}
                onChange={(event) => setPrimary(event.target.value)}
                autoComplete="off"
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium" htmlFor="paging-backup">
                Backup number (optional)
              </label>
              <Input
                id="paging-backup"
                type="tel"
                placeholder="+15557654321"
                value={backup}
                onChange={(event) => setBackup(event.target.value)}
                autoComplete="off"
              />
              <p className="text-xs text-muted-foreground">
                Full international format. Without a backup, an emergency the primary does not
                acknowledge is marked unresolved.
              </p>
            </div>
            <div className="space-y-2">
              <span className="text-sm font-medium">Channels</span>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={smsEnabled}
                  onChange={(event) => setSmsEnabled(event.target.checked)}
                />
                Text message (includes an acknowledgement link)
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={voiceEnabled}
                  onChange={(event) => setVoiceEnabled(event.target.checked)}
                />
                Automated voice call (answering it does not acknowledge)
              </label>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium" htmlFor="paging-timeout">
                Minutes to acknowledge before escalating
              </label>
              <Input
                id="paging-timeout"
                type="number"
                min={1}
                max={60}
                value={timeoutMinutes}
                onChange={(event) => setTimeoutMinutes(event.target.value)}
              />
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={isEnabled}
                onChange={(event) => setIsEnabled(event.target.checked)}
              />
              Emergency paging enabled
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
