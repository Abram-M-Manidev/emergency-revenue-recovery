"use client";

/**
 * Where this organization's emergency alerts go.
 *
 * The assistant may only tell an emergency caller that a dispatcher has been
 * alerted when a configured endpoint actually accepted the message. Until
 * something is configured here, every emergency call truthfully says the
 * alert could not be confirmed — so this form is the difference between a
 * pilot that can answer emergencies and one that can only log them.
 *
 * The destination is never displayed once saved. The API returns a masked
 * hint and has no field carrying the real URL, because a Slack or Teams
 * incoming-webhook URL is the entire credential.
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
  configureNotificationSettings,
  deleteNotificationSettings,
  fetchNotificationSettings,
  setNotificationsEnabled,
} from "@/lib/api/organization";
import type { NotificationSettings } from "@/lib/api/types";

interface Props {
  canManage: boolean;
}

export function EmergencyNotificationsCard({ canManage }: Props) {
  const { toast } = useToast();
  const [settings, setSettings] = useState<NotificationSettings | null>(null);
  const [isLoading, setIsLoading] = useState(canManage);
  const [destination, setDestination] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [isToggling, setIsToggling] = useState(false);
  const [isRemoving, setIsRemoving] = useState(false);

  useEffect(() => {
    if (!canManage) return;
    let cancelled = false;
    fetchNotificationSettings()
      .then((loaded) => {
        if (!cancelled) setSettings(loaded);
      })
      .catch(() =>
        toast({ title: "Failed to load notification settings", variant: "destructive" }),
      )
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canManage]);

  async function save() {
    const trimmed = destination.trim();
    if (!trimmed) return;
    setIsSaving(true);
    try {
      const saved = await configureNotificationSettings({
        channel: "webhook",
        destination: trimmed,
        is_enabled: true,
      });
      setSettings(saved);
      // Cleared on success so the credential does not sit in a form field
      // (or in React state) any longer than the request needs it.
      setDestination("");
      toast({ title: "Emergency alerts configured", variant: "success" });
    } catch (error) {
      toast({
        // The API's 422 names the rule that was broken — https required, a
        // private address, credentials in the URL — and never echoes the
        // submitted value back.
        title: error instanceof ApiError ? error.message : "Failed to save destination",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  }

  async function toggle() {
    if (!settings) return;
    setIsToggling(true);
    try {
      const updated = await setNotificationsEnabled(!settings.is_enabled);
      setSettings(updated);
      toast({
        title: updated.is_enabled ? "Emergency alerts resumed" : "Emergency alerts paused",
        variant: "success",
      });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to update alerts",
        variant: "destructive",
      });
    } finally {
      setIsToggling(false);
    }
  }

  async function remove() {
    setIsRemoving(true);
    try {
      await deleteNotificationSettings();
      setSettings(null);
      toast({ title: "Emergency alert destination removed", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to remove destination",
        variant: "destructive",
      });
    } finally {
      setIsRemoving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Emergency alerts</CardTitle>
        <CardDescription>
          Where we notify a human when a caller reports an emergency. Until this is set,
          emergency callers are told their request is logged but that we could not confirm
          anyone was alerted.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!canManage ? (
          <EmptyState
            title="Owner-only settings"
            description="Ask an Owner to configure where emergency alerts are sent."
          />
        ) : isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <div className="space-y-4">
            {settings ? (
              <div className="space-y-3">
                <div className="flex items-center justify-between border-b border-border pb-3 text-sm">
                  <span className="text-muted-foreground">Destination</span>
                  {/* The masked hint, never the URL. */}
                  <span className="font-mono text-xs font-medium">
                    {settings.destination_hint}
                  </span>
                </div>
                <div className="flex items-center justify-between text-sm">
                  <div>
                    <p className="font-medium">Status</p>
                    <p className="text-muted-foreground">
                      {settings.is_enabled
                        ? "A dispatcher is alerted on every emergency call."
                        : "Paused — emergency callers are told the alert could not be confirmed."}
                    </p>
                  </div>
                  <div className="flex items-center gap-3">
                    <Badge variant={settings.is_enabled ? "success" : "secondary"}>
                      {settings.is_enabled ? "Active" : "Paused"}
                    </Badge>
                    <Button
                      variant={settings.is_enabled ? "outline" : "primary"}
                      size="sm"
                      onClick={toggle}
                      isLoading={isToggling}
                    >
                      {settings.is_enabled ? "Pause" : "Resume"}
                    </Button>
                  </div>
                </div>
              </div>
            ) : (
              <EmptyState
                title="No destination configured"
                description="Emergency requests are still recorded for your team, but nobody is actively notified."
              />
            )}

            <div className="space-y-2 border-t border-border pt-4">
              <label className="text-sm font-medium" htmlFor="notification-destination">
                {settings ? "Replace destination" : "Webhook URL"}
              </label>
              <div className="flex items-end gap-2">
                <Input
                  id="notification-destination"
                  type="url"
                  placeholder="https://hooks.example.com/services/..."
                  value={destination}
                  onChange={(event) => setDestination(event.target.value)}
                  autoComplete="off"
                />
                <Button onClick={save} isLoading={isSaving} disabled={!destination.trim()}>
                  Save
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                An HTTPS endpoint your team already watches — a Slack or Teams incoming
                webhook, PagerDuty, or anything that accepts JSON. Treated as a secret: it is
                never shown again after saving, and never appears in logs. Keep your own copy
                somewhere safe.
              </p>
              {settings ? (
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={remove}
                  isLoading={isRemoving}
                >
                  Remove destination
                </Button>
              ) : null}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
