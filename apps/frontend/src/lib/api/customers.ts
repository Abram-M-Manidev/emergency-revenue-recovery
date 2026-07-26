import { apiRequest } from "@/lib/api/client";
import type { Customer, CustomerHistory, CustomerPayload } from "@/lib/api/types";

const BASE = "/customers";

export function fetchCustomers(search?: string): Promise<Customer[]> {
  const query = search ? `?search=${encodeURIComponent(search)}` : "";
  return apiRequest<Customer[]>(`${BASE}${query}`);
}

export function fetchCustomerHistory(customerId: string): Promise<CustomerHistory> {
  return apiRequest<CustomerHistory>(`${BASE}/${customerId}`);
}

export function createCustomer(payload: CustomerPayload): Promise<Customer> {
  return apiRequest<Customer>(BASE, { method: "POST", body: payload });
}

export function updateCustomer(customerId: string, payload: CustomerPayload): Promise<Customer> {
  return apiRequest<Customer>(`${BASE}/${customerId}`, { method: "PATCH", body: payload });
}
