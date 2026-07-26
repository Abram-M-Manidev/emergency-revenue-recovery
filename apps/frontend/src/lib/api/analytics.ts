import { apiRequest } from "@/lib/api/client";
import type { AnalyticsSummary, DateRangePreset } from "@/lib/api/types";

const BASE = "/analytics";

export function fetchAnalyticsSummary(range: DateRangePreset): Promise<AnalyticsSummary> {
  return apiRequest<AnalyticsSummary>(`${BASE}/summary?range=${range}`);
}
