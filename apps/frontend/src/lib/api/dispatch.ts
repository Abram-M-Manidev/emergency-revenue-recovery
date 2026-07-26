import { apiRequest } from "@/lib/api/client";
import type {
  CreateTechnicianPayload,
  EmergencyTicket,
  TechnicianProfile,
  TicketStatus,
} from "@/lib/api/types";

const BASE = "/dispatch";

// --- Tickets ---

export function fetchTickets(status?: TicketStatus): Promise<EmergencyTicket[]> {
  const query = status ? `?status=${status}` : "";
  return apiRequest<EmergencyTicket[]>(`${BASE}/tickets${query}`);
}

export function fetchTicket(ticketId: string): Promise<EmergencyTicket> {
  return apiRequest<EmergencyTicket>(`${BASE}/tickets/${ticketId}`);
}

export function assignTicket(
  ticketId: string,
  technicianUserId: string,
): Promise<EmergencyTicket> {
  return apiRequest<EmergencyTicket>(`${BASE}/tickets/${ticketId}/assign`, {
    method: "POST",
    body: { technician_user_id: technicianUserId },
  });
}

export function updateTicketStatus(
  ticketId: string,
  status: TicketStatus,
  actualValue?: number,
): Promise<EmergencyTicket> {
  return apiRequest<EmergencyTicket>(`${BASE}/tickets/${ticketId}/status`, {
    method: "POST",
    body: { status, actual_value: actualValue ?? null },
  });
}

// --- Technicians ---

export function fetchTechnicians(onCallOnly = false): Promise<TechnicianProfile[]> {
  const query = onCallOnly ? "?on_call_only=true" : "";
  return apiRequest<TechnicianProfile[]>(`${BASE}/technicians${query}`);
}

export function createTechnician(
  payload: CreateTechnicianPayload,
): Promise<TechnicianProfile> {
  return apiRequest<TechnicianProfile>(`${BASE}/technicians`, { method: "POST", body: payload });
}

export function setTechnicianOnCall(
  userId: string,
  isOnCall: boolean,
): Promise<TechnicianProfile> {
  return apiRequest<TechnicianProfile>(`${BASE}/technicians/${userId}/on-call`, {
    method: "PATCH",
    body: { is_on_call: isOnCall },
  });
}
