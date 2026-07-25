"use client";

import { AppointmentQueue } from "@/components/appointments/appointment-queue";

export default function AppointmentsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Appointments</h1>
        <p className="text-sm text-muted-foreground">
          Turn AI-requested appointments into scheduled, technician-assigned visits.
        </p>
      </div>

      <AppointmentQueue />
    </div>
  );
}
