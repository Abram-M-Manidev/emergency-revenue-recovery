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
  default_duration_minutes: number | null;
  default_price: number | null;
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

// --- AI Brain conversations ---

export type ConversationChannel = "text" | "voice";
export type ConversationStatus = "active" | "completed";

export interface Conversation {
  id: string;
  channel: ConversationChannel;
  status: ConversationStatus;
  caller_phone_number: string | null;
  started_at: string;
  ended_at: string | null;
  created_at: string;
  updated_at: string;
}

export type MessageRole = "customer" | "assistant";

export interface ConversationMessage {
  id: string;
  role: MessageRole;
  content: string;
  created_at: string;
}

export type CallClassification = "emergency" | "non_emergency" | "unknown";
export type RecommendedAction =
  | "create_emergency_ticket"
  | "book_appointment"
  | "answer_faq"
  | "escalate_to_human"
  | "none";

export interface ConversationOutcome {
  id: string;
  classification: CallClassification;
  confidence: number;
  recommended_action: RecommendedAction;
  matched_service_id: string | null;
  customer_name: string | null;
  customer_phone: string | null;
  customer_address: string | null;
  summary: string;
  updated_at: string;
}

export interface SendMessageResult {
  conversation: Conversation;
  reply: ConversationMessage;
  outcome: ConversationOutcome;
}

export interface ConversationDetail {
  conversation: Conversation;
  messages: ConversationMessage[];
  outcome: ConversationOutcome | null;
}

// --- Voice ---

export type VoiceProvider = "vapi";

export interface VoiceLine {
  id: string;
  provider: VoiceProvider;
  phone_number: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface VoiceCall {
  id: string;
  conversation_id: string;
  caller_number: string | null;
  started_at: string;
  ended_at: string | null;
  ended_reason: string | null;
  duration_seconds: number | null;
  recording_url: string | null;
}

// --- Dispatch ---

export type TicketStatus = "new" | "assigned" | "en_route" | "resolved" | "canceled";

export interface EmergencyTicket {
  id: string;
  conversation_id: string;
  matched_service_id: string | null;
  status: TicketStatus;
  customer_name: string | null;
  customer_phone: string | null;
  customer_address: string | null;
  summary: string;
  assigned_technician_user_id: string | null;
  assigned_at: string | null;
  closed_at: string | null;
  created_at: string;
  updated_at: string;
  actual_value: number | null;
}

export interface TechnicianProfile {
  id: string;
  user_id: string;
  full_name: string | null;
  email: string | null;
  phone_number: string;
  is_on_call: boolean;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateTechnicianPayload {
  full_name: string;
  email: string;
  phone_number: string;
  temporary_password: string;
}

// --- Appointments ---

export type AppointmentStatus = "requested" | "scheduled" | "completed" | "canceled" | "no_show";

export interface Appointment {
  id: string;
  conversation_id: string;
  matched_service_id: string | null;
  status: AppointmentStatus;
  customer_name: string | null;
  customer_phone: string | null;
  customer_address: string | null;
  summary: string;
  scheduled_start_at: string | null;
  duration_minutes: number | null;
  assigned_technician_user_id: string | null;
  assigned_at: string | null;
  closed_at: string | null;
  created_at: string;
  updated_at: string;
  actual_value: number | null;
}

export interface ScheduleAppointmentPayload {
  scheduled_start_at: string;
  duration_minutes: number;
  technician_user_id: string | null;
}

// --- Customers ---

export interface Customer {
  id: string;
  full_name: string | null;
  phone_number: string;
  email: string | null;
  address: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface CustomerHistory {
  customer: Customer;
  tickets: EmergencyTicket[];
  appointments: Appointment[];
}

export interface CustomerPayload {
  full_name: string | null;
  phone_number: string;
  email: string | null;
  address: string | null;
  notes: string | null;
}

// --- Analytics ---

export type DateRangePreset = "today" | "7d" | "30d" | "90d" | "all";

export interface DailyCount {
  day: string;
  count: number;
}

export interface DailyRevenue {
  day: string;
  amount: number;
}

export interface BucketCount {
  label: string;
  count: number;
}

export interface AnalyticsSummary {
  range_start: string | null;
  range_end: string;
  total_conversations: number;
  conversations_by_day: DailyCount[];
  conversations_by_channel: BucketCount[];
  classification_breakdown: BucketCount[];
  recommended_action_breakdown: BucketCount[];
  tickets_created: number;
  tickets_resolved: number;
  average_ticket_resolution_minutes: number | null;
  appointments_created: number;
  appointments_completed: number;
  appointments_no_show: number;
  appointment_show_up_rate: number | null;
  appointment_status_breakdown: BucketCount[];
  new_customers: number;
  total_customers: number;
  ticket_revenue: number;
  appointment_revenue: number;
  total_revenue: number;
  revenue_by_day: DailyRevenue[];
}
