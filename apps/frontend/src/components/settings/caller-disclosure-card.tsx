"use client";

/**
 * What callers are told about the assistant and recording.
 *
 * ERRS says it — word for word, before its first reply on every call — so
 * it never depends on the AI remembering to. What ERRS cannot control: a
 * greeting set in the Vapi dashboard is spoken before ERRS hears the call,
 * and whether calls are recorded at all is a Vapi setting. Shows the exact
 * sentences callers will hear.
 */

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api/client";
import {
  configureCallDisclosureSettings,
  fetchCallDisclosureSettings,
  resetCallDisclosureSettings,
} from "@/lib/api/organization";
import type { CallDisclosureSettings } from "@/lib/api/types";

interface Props {
  canManage: boolean;
}

export function CallerDisclosureCard({ canManage }: Props) {
  const { toast } = useToast();
  const [settings, setSettings] = useState<CallDisclosureSettings | null>(null);
  const [isLoading, setIsLoading] = useState(canManage);
  const [aiDisclosure, setAiDisclosure] = useState(true);
  const [recordingNotice, setRecordingNotice] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  function load(loaded: CallDisclosureSettings) {
    setSettings(loaded);
    setAiDisclosure(loaded.ai_disclosure_enabled);
    setRecordingNotice(loaded.recording_notice_enabled);
  }

  useEffect(() => {
    if (!canManage) return;
    let cancelled = false;
    fetchCallDisclosureSettings()
      .then((loaded) => {
        if (!cancelled) load(loaded);
      })
      .catch(() => toast({ title: "Failed to load caller notice settings", variant: "destructive" }))
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
      load(
        await configureCallDisclosureSettings({
          ai_disclosure_enabled: aiDisclosure,
          recording_notice_enabled: recordingNotice,
        }),
      );
      toast({ title: "Caller notice saved", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to save caller notice",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  }

  async function reset() {
    setIsSaving(true);
    try {
      await resetCallDisclosureSettings();
      load(await fetchCallDisclosureSettings());
      toast({ title: "Caller notice reset to default", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to reset caller notice",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Caller notice</CardTitle>
        <CardDescription>
          What every caller is told about the automated assistant and about recording. The system
          says it word for word before the assistant&apos;s first reply — it never depends on the
          AI. Decide with your own advisers which notices your business needs.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!canManage ? (
          <EmptyState
            title="Owner-only settings"
            description="Ask an Owner to configure what callers are told."
          />
        ) : isLoading || !settings ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <div className="space-y-4">
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">Status</span>
              <Badge variant={settings.is_default ? "secondary" : "success"}>
                {settings.is_default ? "Default (both notices)" : "Customised"}
              </Badge>
            </div>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="mt-1"
                checked={aiDisclosure}
                onChange={(event) => setAiDisclosure(event.target.checked)}
              />
              <span>Tell callers they are speaking with an automated assistant</span>
            </label>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="mt-1"
                checked={recordingNotice}
                onChange={(event) => setRecordingNotice(event.target.checked)}
              />
              <span>
                Tell callers the call is recorded
                <span className="block text-xs text-muted-foreground">
                  Recording itself is switched on or off in your Vapi assistant, not here. Keep
                  this on whenever calls are recorded — a recording made without this notice is
                  flagged on the call.
                </span>
              </span>
            </label>
            <div className="space-y-1 rounded-md border border-border bg-muted/30 p-3 text-sm">
              <div className="text-xs font-medium text-muted-foreground">Callers currently hear</div>
              <div>{settings.disclosure_sentence ?? "(nothing — both notices are off)"}</div>
              <div className="pt-2 text-xs font-medium text-muted-foreground">
                Opening, when the assistant speaks first
              </div>
              <div>{settings.opening_message}</div>
            </div>
            <div className="flex gap-2 border-t border-border pt-4">
              <Button onClick={save} isLoading={isSaving}>
                Save
              </Button>
              {!settings.is_default ? (
                <Button variant="outline" size="sm" onClick={reset} isLoading={isSaving}>
                  Reset to default
                </Button>
              ) : null}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
