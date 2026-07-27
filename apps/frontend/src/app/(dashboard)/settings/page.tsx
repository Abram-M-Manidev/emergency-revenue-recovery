"use client";

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/hooks/use-auth";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api/client";
import { fetchCurrentOrganization, updateCurrentOrganization } from "@/lib/api/organization";
import { fetchVoiceLine } from "@/lib/api/voice";
import type { Organization, VoiceLine } from "@/lib/api/types";

export default function SettingsPage() {
  const { user } = useAuth();
  const { toast } = useToast();
  const [voiceLine, setVoiceLine] = useState<VoiceLine | null>(null);
  const [isLoadingVoiceLine, setIsLoadingVoiceLine] = useState(true);

  const canManageOrganization = user?.permissions.includes("organization:manage") ?? false;
  const [organization, setOrganization] = useState<Organization | null>(null);
  const [isLoadingOrganization, setIsLoadingOrganization] = useState(canManageOrganization);
  const [orgName, setOrgName] = useState("");
  const [isSavingName, setIsSavingName] = useState(false);
  const [isTogglingActive, setIsTogglingActive] = useState(false);

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

  useEffect(() => {
    if (!canManageOrganization) return;
    let cancelled = false;
    fetchCurrentOrganization()
      .then((org) => {
        if (!cancelled) {
          setOrganization(org);
          setOrgName(org.name);
        }
      })
      .catch(() => toast({ title: "Failed to load organization", variant: "destructive" }))
      .finally(() => {
        if (!cancelled) setIsLoadingOrganization(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canManageOrganization]);

  async function saveName() {
    if (!orgName.trim()) return;
    setIsSavingName(true);
    try {
      const updated = await updateCurrentOrganization({ name: orgName.trim() });
      setOrganization(updated);
      toast({ title: "Organization renamed", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to rename organization",
        variant: "destructive",
      });
    } finally {
      setIsSavingName(false);
    }
  }

  async function toggleActive() {
    if (!organization) return;
    setIsTogglingActive(true);
    try {
      const updated = await updateCurrentOrganization({ is_active: !organization.is_active });
      setOrganization(updated);
      toast({
        title: updated.is_active ? "Organization reactivated" : "Organization deactivated",
        variant: "success",
      });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to update organization",
        variant: "destructive",
      });
    } finally {
      setIsTogglingActive(false);
    }
  }

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
          <CardTitle className="text-base">Organization</CardTitle>
          <CardDescription>
            {canManageOrganization
              ? "Rename your organization or pause it entirely. Billing and integrations arrive in a later milestone."
              : "Team management, roles, and billing."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!canManageOrganization ? (
            <EmptyState
              title="Owner-only settings"
              description="Ask an Owner to manage the organization's name or status. Team invitations live on the Team page."
            />
          ) : isLoadingOrganization ? (
            <Skeleton className="h-24 w-full" />
          ) : organization ? (
            <div className="space-y-4">
              <div className="flex items-end gap-2">
                <div className="flex-1">
                  <label className="text-sm font-medium">Organization name</label>
                  <Input value={orgName} onChange={(event) => setOrgName(event.target.value)} />
                </div>
                <Button
                  onClick={saveName}
                  isLoading={isSavingName}
                  disabled={orgName.trim() === organization.name}
                >
                  Save
                </Button>
              </div>
              <div className="flex items-center justify-between border-t border-border pt-4 text-sm">
                <div>
                  <p className="font-medium">Status</p>
                  <p className="text-muted-foreground">
                    Deactivating blocks every teammate from logging in.
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  <Badge variant={organization.is_active ? "success" : "secondary"}>
                    {organization.is_active ? "Active" : "Inactive"}
                  </Badge>
                  <Button
                    variant={organization.is_active ? "destructive" : "outline"}
                    size="sm"
                    onClick={toggleActive}
                    isLoading={isTogglingActive}
                  >
                    {organization.is_active ? "Deactivate" : "Reactivate"}
                  </Button>
                </div>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
