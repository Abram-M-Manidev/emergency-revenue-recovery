import { apiRequest } from "@/lib/api/client";
import type { AnalyticsSummary, DateRangePreset, UsageSummary } from "@/lib/api/types";

const BASE = "/analytics";

export function fetchAnalyticsSummary(range: DateRangePreset): Promise<AnalyticsSummary> {
  return apiRequest<AnalyticsSummary>(`${BASE}/summary?range=${range}`);
}

/** Metering M3: the signed-in business's usage for one billing period (YYYY-MM). */
export function fetchUsageSummary(period: string): Promise<UsageSummary> {
  return apiRequest<UsageSummary>(`/usage/summary?period=${encodeURIComponent(period)}`);
}
