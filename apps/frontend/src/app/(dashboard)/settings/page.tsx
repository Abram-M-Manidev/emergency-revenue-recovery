"use client";

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/hooks/use-auth";
import { fetchVoiceLine } from "@/lib/api/voice";
import type { VoiceLine } from "@/lib/api/types";

export default function SettingsPage() {
  const { user } = useAuth();
  const [voiceLine, setVoiceLine] = useState<VoiceLine | null>(null);
  const [isLoadingVoiceLine, setIsLoadingVoiceLine] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetchVoiceLine()
      .then((line) => {
        if (!cancelled) setVoiceLine(line);
      })
      .catch(() => {
        /* treated the same as "not configured yet" below */
      })
      .finally(() => {
        if (!cancelled) setIsLoadingVoiceLine(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">Manage your account and organization.</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Profile</CardTitle>
          <CardDescription>Your account details.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-center justify-between border-b border-border pb-3 text-sm">
            <span className="text-muted-foreground">Full name</span>
            <span className="font-medium">{user?.full_name}</span>
          </div>
          <div className="flex items-center justify-between border-b border-border pb-3 text-sm">
            <span className="text-muted-foreground">Email</span>
            <span className="font-medium">{user?.email}</span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-muted-foreground">Roles</span>
            <div className="flex gap-1.5">
              {user?.roles.map((role) => (
                <Badge key={role} variant="secondary">
                  {role}
                </Badge>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Voice line</CardTitle>
          <CardDescription>
            The phone number the AI Brain answers after hours.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoadingVoiceLine ? (
            <Skeleton className="h-16 w-full" />
          ) : voiceLine ? (
            <div className="space-y-3">
              <div className="flex items-center justify-between border-b border-border pb-3 text-sm">
                <span className="text-muted-foreground">Phone number</span>
                <span className="font-medium">{voiceLine.phone_number ?? "—"}</span>
              </div>
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground">Status</span>
                <Badge variant={voiceLine.is_active ? "success" : "secondary"}>
                  {voiceLine.is_active ? "Active" : "Inactive"}
                </Badge>
              </div>
            </div>
          ) : (
            <EmptyState
              title="Not yet configured"
              description="Contact support to enable AI phone answering for your business."
            />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Organization &amp; billing</CardTitle>
          <CardDescription>Team management, roles, and billing.</CardDescription>
        </CardHeader>
        <CardContent>
          <EmptyState
            title="More settings coming soon"
            description="Team invitations, billing, and integrations arrive in later milestones."
          />
        </CardContent>
      </Card>
    </div>
  );
}
