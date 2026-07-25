"use client";

import { useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  fetchAppointments,
  scheduleAppointment,
  updateAppointmentStatus,
} from "@/lib/api/appointments";
import { fetchTechnicians } from "@/lib/api/dispatch";
import { ApiError } from "@/lib/api/client";
import type { Appointment, AppointmentStatus, TechnicianProfile } from "@/lib/api/types";
import { useToast } from "@/hooks/use-toast";

const STATUS_FILTERS: { value: AppointmentStatus | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "requested", label: "Requested" },
  { value: "scheduled", label: "Scheduled" },
  { value: "completed", label: "Completed" },
  { value: "canceled", label: "Canceled" },
  { value: "no_show", label: "No-show" },
];

const STATUS_BADGE_VARIANT: Record<AppointmentStatus, "destructive" | "default" | "success" | "secondary" | "outline"> = {
  requested: "destructive",
  scheduled: "default",
  completed: "success",
  canceled: "secondary",
  no_show: "outline",
};

const STATUS_LABEL: Record<AppointmentStatus, string> = {
  requested: "Requested",
  scheduled: "Scheduled",
  completed: "Completed",
  canceled: "Canceled",
  no_show: "No-show",
};

interface ScheduleFormState {
  scheduled_start_at: string;
  duration_minutes: string;
  technician_user_id: string;
}

const EMPTY_SCHEDULE_FORM: ScheduleFormState = {
  scheduled_start_at: "",
  duration_minutes: "",
  technician_user_id: "",
};

function toDatetimeLocalValue(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function AppointmentQueue() {
  const { toast } = useToast();
  const [isLoading, setIsLoading] = useState(true);
  const [appointments, setAppointments] = useState<Appointment[]>([]);
  const [technicians, setTechnicians] = useState<TechnicianProfile[]>([]);
  const [statusFilter, setStatusFilter] = useState<AppointmentStatus | "all">("all");
  const [modalAppointment, setModalAppointment] = useState<Appointment | null>(null);
  const [form, setForm] = useState<ScheduleFormState>(EMPTY_SCHEDULE_FORM);
  const [isSaving, setIsSaving] = useState(false);

  const technicianById = useMemo(
    () => new Map(technicians.map((t) => [t.user_id, t])),
    [technicians],
  );

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    Promise.all([
      fetchAppointments(statusFilter === "all" ? undefined : statusFilter),
      fetchTechnicians(),
    ])
      .then(([appointmentData, technicianData]) => {
        if (cancelled) return;
        setAppointments(appointmentData);
        setTechnicians(technicianData);
      })
      .catch(() => toast({ title: "Failed to load appointments", variant: "destructive" }))
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter]);

  function replaceAppointment(updated: Appointment) {
    setAppointments((current) => current.map((a) => (a.id === updated.id ? updated : a)));
  }

  function openSchedule(appointment: Appointment) {
    setModalAppointment(appointment);
    setForm({
      scheduled_start_at: toDatetimeLocalValue(appointment.scheduled_start_at),
      duration_minutes: appointment.duration_minutes ? String(appointment.duration_minutes) : "",
      technician_user_id: appointment.assigned_technician_user_id ?? "",
    });
  }

  async function saveSchedule() {
    if (!modalAppointment) return;
    if (!form.scheduled_start_at || !form.duration_minutes) {
      toast({ title: "Date/time and duration are required", variant: "destructive" });
      return;
    }
    setIsSaving(true);
    try {
      const updated = await scheduleAppointment(modalAppointment.id, {
        scheduled_start_at: new Date(form.scheduled_start_at).toISOString(),
        duration_minutes: Number(form.duration_minutes),
        technician_user_id: form.technician_user_id || null,
      });
      replaceAppointment(updated);
      setModalAppointment(null);
      toast({ title: "Appointment scheduled", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to schedule appointment",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  }

  async function handleStatusChange(appointment: Appointment, status: AppointmentStatus) {
    try {
      const updated = await updateAppointmentStatus(appointment.id, status);
      replaceAppointment(updated);
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to update appointment",
        variant: "destructive",
      });
    }
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="text-base">Appointments</CardTitle>
          <CardDescription>
            Requested automatically when the AI Brain books a non-emergency appointment.
          </CardDescription>
        </div>
        <select
          className="h-9 rounded-md border border-input bg-background px-2 text-sm"
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value as AppointmentStatus | "all")}
        >
          {STATUS_FILTERS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : appointments.length === 0 ? (
          <EmptyState
            title="No appointments"
            description="Appointments requested from AI Brain calls will appear here."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Customer</TableHead>
                <TableHead>Summary</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Scheduled for</TableHead>
                <TableHead>Duration</TableHead>
                <TableHead>Assigned to</TableHead>
                <TableHead>Created</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {appointments.map((appointment) => {
                const assignedTechnician = appointment.assigned_technician_user_id
                  ? technicianById.get(appointment.assigned_technician_user_id)
                  : undefined;
                return (
                  <TableRow key={appointment.id}>
                    <TableCell>
                      <div className="font-medium">{appointment.customer_name ?? "Unknown caller"}</div>
                      <div className="text-xs text-muted-foreground">
                        {appointment.customer_phone ?? "—"}
                        {appointment.customer_address ? ` · ${appointment.customer_address}` : ""}
                      </div>
                    </TableCell>
                    <TableCell className="max-w-xs truncate" title={appointment.summary}>
                      {appointment.summary}
                    </TableCell>
                    <TableCell>
                      <Badge variant={STATUS_BADGE_VARIANT[appointment.status]}>
                        {STATUS_LABEL[appointment.status]}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {appointment.scheduled_start_at
                        ? new Date(appointment.scheduled_start_at).toLocaleString()
                        : "—"}
                    </TableCell>
                    <TableCell>
                      {appointment.duration_minutes ? `${appointment.duration_minutes} min` : "—"}
                    </TableCell>
                    <TableCell>{assignedTechnician?.full_name ?? "—"}</TableCell>
                    <TableCell>{new Date(appointment.created_at).toLocaleString()}</TableCell>
                    <TableCell>
                      {appointment.status === "requested" ? (
                        <div className="flex gap-2">
                          <Button size="sm" onClick={() => openSchedule(appointment)}>
                            Schedule
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => handleStatusChange(appointment, "canceled")}
                          >
                            Cancel
                          </Button>
                        </div>
                      ) : appointment.status === "scheduled" ? (
                        <div className="flex flex-wrap gap-2">
                          <Button size="sm" variant="outline" onClick={() => openSchedule(appointment)}>
                            Reschedule
                          </Button>
                          <Button
                            size="sm"
                            onClick={() => handleStatusChange(appointment, "completed")}
                          >
                            Mark completed
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => handleStatusChange(appointment, "no_show")}
                          >
                            Mark no-show
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => handleStatusChange(appointment, "canceled")}
                          >
                            Cancel
                          </Button>
                        </div>
                      ) : null}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>

      <Modal
        open={modalAppointment !== null}
        onOpenChange={(open) => {
          if (!open) setModalAppointment(null);
        }}
        title={modalAppointment?.status === "scheduled" ? "Reschedule appointment" : "Schedule appointment"}
        description="Pick a date/time within business hours and, optionally, a technician."
        footer={
          <>
            <Button variant="outline" onClick={() => setModalAppointment(null)}>
              Cancel
            </Button>
            <Button onClick={saveSchedule} isLoading={isSaving}>
              Save
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="text-sm font-medium">Date &amp; time</label>
            <Input
              type="datetime-local"
              value={form.scheduled_start_at}
              onChange={(event) =>
                setForm((current) => ({ ...current, scheduled_start_at: event.target.value }))
              }
            />
          </div>
          <div>
            <label className="text-sm font-medium">Duration (minutes)</label>
            <Input
              type="number"
              min={1}
              max={1440}
              value={form.duration_minutes}
              onChange={(event) =>
                setForm((current) => ({ ...current, duration_minutes: event.target.value }))
              }
            />
          </div>
          <div>
            <label className="text-sm font-medium">Technician</label>
            <select
              className="h-9 w-full rounded-md border border-input bg-background px-2 text-sm"
              value={form.technician_user_id}
              onChange={(event) =>
                setForm((current) => ({ ...current, technician_user_id: event.target.value }))
              }
            >
              <option value="">Unassigned</option>
              {technicians.map((technician) => (
                <option key={technician.user_id} value={technician.user_id}>
                  {technician.full_name ?? technician.phone_number}
                </option>
              ))}
            </select>
          </div>
        </div>
      </Modal>
    </Card>
  );
}
