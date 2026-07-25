"use client";

import { TechnicianRoster } from "@/components/dispatch/technician-roster";
import { TicketQueue } from "@/components/dispatch/ticket-queue";

export default function DispatchPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Emergency Dispatch</h1>
        <p className="text-sm text-muted-foreground">
          Track AI-flagged emergencies through to resolution and manage who they get assigned to.
        </p>
      </div>

      <TicketQueue />
      <TechnicianRoster />
    </div>
  );
}
