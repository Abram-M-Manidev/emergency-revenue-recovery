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
