import { apiRequest } from "@/lib/api/client";
import type {
  Appointment,
  AppointmentStatus,
  ScheduleAppointmentPayload,
} from "@/lib/api/types";

const BASE = "/appointments";

export function fetchAppointments(status?: AppointmentStatus): Promise<Appointment[]> {
  const query = status ? `?status=${status}` : "";
  return apiRequest<Appointment[]>(`${BASE}${query}`);
}

export function fetchAppointment(appointmentId: string): Promise<Appointment> {
  return apiRequest<Appointment>(`${BASE}/${appointmentId}`);
}

export function scheduleAppointment(
  appointmentId: string,
  payload: ScheduleAppointmentPayload,
): Promise<Appointment> {
  return apiRequest<Appointment>(`${BASE}/${appointmentId}/schedule`, {
    method: "POST",
    body: payload,
  });
}

export function updateAppointmentStatus(
  appointmentId: string,
  status: AppointmentStatus,
): Promise<Appointment> {
  return apiRequest<Appointment>(`${BASE}/${appointmentId}/status`, {
    method: "POST",
    body: { status },
  });
}
