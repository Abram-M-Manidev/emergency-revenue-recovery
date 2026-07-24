import { apiRequest } from "@/lib/api/client";
import type {
  BusinessProfile,
  CreateEmergencyKeywordPayload,
  CreateHoursExceptionPayload,
  CreateServiceAreaPayload,
  EmergencyKeyword,
  FAQEntry,
  FAQPayload,
  HoursException,
  Service,
  ServiceArea,
  ServicePayload,
  UpdateBusinessProfilePayload,
  WeeklyHours,
  WeeklyHoursEntry,
} from "@/lib/api/types";

const BASE = "/business-knowledge";

// --- Business profile ---

export function fetchBusinessProfile(): Promise<BusinessProfile> {
  return apiRequest<BusinessProfile>(`${BASE}/profile`);
}

export function updateBusinessProfile(
  payload: UpdateBusinessProfilePayload,
): Promise<BusinessProfile> {
  return apiRequest<BusinessProfile>(`${BASE}/profile`, { method: "PUT", body: payload });
}

// --- Hours ---

export function fetchWeeklyHours(): Promise<WeeklyHours[]> {
  return apiRequest<WeeklyHours[]>(`${BASE}/hours`);
}

export function replaceWeeklyHours(entries: WeeklyHoursEntry[]): Promise<WeeklyHours[]> {
  return apiRequest<WeeklyHours[]>(`${BASE}/hours`, { method: "PUT", body: { entries } });
}

export function fetchHoursExceptions(): Promise<HoursException[]> {
  return apiRequest<HoursException[]>(`${BASE}/hours/exceptions`);
}

export function createHoursException(
  payload: CreateHoursExceptionPayload,
): Promise<HoursException> {
  return apiRequest<HoursException>(`${BASE}/hours/exceptions`, { method: "POST", body: payload });
}

export function deleteHoursException(exceptionId: string): Promise<void> {
  return apiRequest<void>(`${BASE}/hours/exceptions/${exceptionId}`, { method: "DELETE" });
}

// --- Service areas ---

export function fetchServiceAreas(): Promise<ServiceArea[]> {
  return apiRequest<ServiceArea[]>(`${BASE}/service-areas`);
}

export function createServiceArea(payload: CreateServiceAreaPayload): Promise<ServiceArea> {
  return apiRequest<ServiceArea>(`${BASE}/service-areas`, { method: "POST", body: payload });
}

export function deleteServiceArea(areaId: string): Promise<void> {
  return apiRequest<void>(`${BASE}/service-areas/${areaId}`, { method: "DELETE" });
}

// --- Services offered ---

export function fetchServices(): Promise<Service[]> {
  return apiRequest<Service[]>(`${BASE}/services`);
}

export function createService(payload: ServicePayload): Promise<Service> {
  return apiRequest<Service>(`${BASE}/services`, { method: "POST", body: payload });
}

export function updateService(serviceId: string, payload: ServicePayload): Promise<Service> {
  return apiRequest<Service>(`${BASE}/services/${serviceId}`, { method: "PATCH", body: payload });
}

export function deleteService(serviceId: string): Promise<void> {
  return apiRequest<void>(`${BASE}/services/${serviceId}`, { method: "DELETE" });
}

// --- Emergency keywords ---

export function fetchEmergencyKeywords(): Promise<EmergencyKeyword[]> {
  return apiRequest<EmergencyKeyword[]>(`${BASE}/emergency-keywords`);
}

export function createEmergencyKeyword(
  payload: CreateEmergencyKeywordPayload,
): Promise<EmergencyKeyword> {
  return apiRequest<EmergencyKeyword>(`${BASE}/emergency-keywords`, {
    method: "POST",
    body: payload,
  });
}

export function deleteEmergencyKeyword(keywordId: string): Promise<void> {
  return apiRequest<void>(`${BASE}/emergency-keywords/${keywordId}`, { method: "DELETE" });
}

// --- FAQs ---

export function fetchFAQs(): Promise<FAQEntry[]> {
  return apiRequest<FAQEntry[]>(`${BASE}/faqs`);
}

export function createFAQ(payload: FAQPayload): Promise<FAQEntry> {
  return apiRequest<FAQEntry>(`${BASE}/faqs`, { method: "POST", body: payload });
}

export function updateFAQ(faqId: string, payload: FAQPayload): Promise<FAQEntry> {
  return apiRequest<FAQEntry>(`${BASE}/faqs/${faqId}`, { method: "PATCH", body: payload });
}

export function deleteFAQ(faqId: string): Promise<void> {
  return apiRequest<void>(`${BASE}/faqs/${faqId}`, { method: "DELETE" });
}
