"use client";

/**
 * The per-tenant voice kill switch.
 *
 * Separate from the organization's own Active/Inactive state on purpose, and
 * the copy here has to make that distinction obvious: deactivating the
 * organization locks every teammate out of the dashboard, while this stops
 * only the phone assistant. Someone reaching for this during an incident is
 * reaching for the narrow one, and should not have to guess which is which.
 *
 * Enforced server-side in `VoiceService` — this control is a convenience for
 * flipping the flag, never the thing that makes it true.
 */

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api/client";
import { updateCurrentOrganization } from "@/lib/api/organization";
import type { Organization } from "@/lib/api/types";

interface Props {
  canManage: boolean;
  organization: Organization | null;
  onChange: (organization: Organization) => void;
}

export function VoiceAssistantCard({ canManage, organization, onChange }: Props) {
  const { toast } = useToast();
  const [isToggling, setIsToggling] = useState(false);

  async function toggle() {
    if (!organization) return;
    setIsToggling(true);
    try {
      const updated = await updateCurrentOrganization({
        voice_assistant_enabled: !organization.voice_assistant_enabled,
      });
      onChange(updated);
      toast({
        title: updated.voice_assistant_enabled
          ? "Voice assistant re-enabled"
          : "Voice assistant disabled",
        variant: "success",
      });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to update the assistant",
        variant: "destructive",
      });
    } finally {
      setIsToggling(false);
    }
  }

  const enabled = organization?.voice_assistant_enabled ?? true;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Voice assistant</CardTitle>
        <CardDescription>
          Whether the AI answers your inbound calls. Turning it off does not affect your
          dashboard, dispatch queue, appointments, or customer records.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!canManage ? (
          <EmptyState
            title="Owner-only settings"
            description="Ask an Owner to enable or disable the voice assistant."
          />
        ) : !organization ? (
          <EmptyState
            title="Unavailable"
            description="We could not load your organization's settings."
          />
        ) : (
          <div className="flex items-center justify-between text-sm">
            <div className="pr-4">
              <p className="font-medium">Status</p>
              <p className="text-muted-foreground">
                {enabled
                  ? "The AI answers calls to your voice line."
                  : "Callers hear that the automated line is unavailable and are asked to hold for your team. Nothing is recorded from those calls."}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-3">
              <Badge variant={enabled ? "success" : "secondary"}>
                {enabled ? "Answering" : "Disabled"}
              </Badge>
              <Button
                variant={enabled ? "destructive" : "outline"}
                size="sm"
                onClick={toggle}
                isLoading={isToggling}
              >
                {enabled ? "Disable" : "Enable"}
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
