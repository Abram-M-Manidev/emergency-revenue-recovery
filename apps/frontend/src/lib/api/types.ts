export interface UserProfile {
  id: string;
  email: string;
  full_name: string;
  organization_id: string;
  is_superuser: boolean;
  roles: string[];
  permissions: string[];
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface AuthResponse {
  tokens: TokenResponse;
  user: UserProfile;
}

export interface RegisterPayload {
  organization_name: string;
  full_name: string;
  email: string;
  password: string;
}

export interface LoginPayload {
  email: string;
  password: string;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
  request_id: string | null;
}

// --- Business Knowledge ---

export type BusinessType = "hvac" | "plumbing" | "electrical" | "other";

export interface BusinessProfile {
  id: string;
  business_type: BusinessType;
  display_name: string;
  phone_number: string | null;
  timezone: string;
  address_line1: string | null;
  address_line2: string | null;
  city: string | null;
  state: string | null;
  postal_code: string | null;
  country: string;
  website: string | null;
  created_at: string;
  updated_at: string;
}

export type UpdateBusinessProfilePayload = Omit<
  BusinessProfile,
  "id" | "created_at" | "updated_at"
>;

export interface WeeklyHoursEntry {
  day_of_week: number;
  is_closed: boolean;
  open_time: string | null;
  close_time: string | null;
}

export interface WeeklyHours extends WeeklyHoursEntry {
  id: string;
}

export interface HoursException {
  id: string;
  date: string;
  is_closed: boolean;
  open_time: string | null;
  close_time: string | null;
  label: string | null;
}

export type CreateHoursExceptionPayload = Omit<HoursException, "id">;

export interface ServiceArea {
  id: string;
  label: string;
  postal_code: string | null;
  city: string | null;
  state: string | null;
}

export type CreateServiceAreaPayload = Omit<ServiceArea, "id">;

export interface Service {
  id: string;
  name: string;
  description: string | null;
  category: string | null;
  is_emergency_eligible: boolean;
  is_active: boolean;
}

export type ServicePayload = Omit<Service, "id">;

export interface EmergencyKeyword {
  id: string;
  phrase: string;
  notes: string | null;
}

export type CreateEmergencyKeywordPayload = Omit<EmergencyKeyword, "id">;

export interface FAQEntry {
  id: string;
  question: string;
  answer: string;
  category: string | null;
  is_active: boolean;
}

export type FAQPayload = Omit<FAQEntry, "id">;
