"use client";

import { TeamRoster } from "@/components/team/team-roster";

export default function TeamPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Team</h1>
        <p className="text-sm text-muted-foreground">
          Invite teammates and manage who has access to this organization.
        </p>
      </div>

      <TeamRoster />
    </div>
  );
}
