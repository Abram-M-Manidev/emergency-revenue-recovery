"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { useAuth } from "@/hooks/use-auth";

export default function SettingsPage() {
  const { user } = useAuth();

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
