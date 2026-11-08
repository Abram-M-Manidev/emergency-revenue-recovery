"use client";

import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, Clock, PhoneCall, PhoneOff } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import { fetchUsageSummary } from "@/lib/api/analytics";
import { ApiError } from "@/lib/api/client";
import type { UsageSummary } from "@/lib/api/types";

import { StatCard } from "./stat-card";

const EXCLUSION_LABEL: Record<string, string> = {
  under_minimum: "under 10 seconds",
  fault_ended_reason: "provider fault",
  duration_unknown: "duration unknown",
};

/** The last three billing periods (UTC months), newest first, as YYYY-MM. */
function recentPeriods(now: Date = new Date()): string[] {
  return [0, 1, 2].map((back) => {
    const date = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - back, 1));
    return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}`;
  });
}

function periodLabel(period: string): string {
  const [year = 1970, month = 1] = period.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, 1)).toLocaleString(undefined, {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

/**
 * Metering M3 in the dashboard: what this business's calls used in a
 * billing period. A report, not a bill — ESSR charges nothing by itself.
 */
export function UsageSection() {
  const periods = useMemo(() => recentPeriods(), []);
  const [period, setPeriod] = useState<string>(periods[0] ?? "");
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setUsage(null);
    setError(null);
    fetchUsageSummary(period)
      .then((data) => {
        if (!cancelled) setUsage(data);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load usage");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [period]);

  const excluded = usage
    ? Object.entries(usage.excluded_calls)
        .map(([reason, count]) => `${count} ${EXCLUSION_LABEL[reason] ?? reason}`)
        .join(" · ")
    : "";
  const reconciled = usage
    ? Object.values(usage.reconciliation).reduce((total, count) => total + count, 0)
    : 0;

  return (
    <section aria-labelledby="usage-title" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="usage-title" className="text-lg font-semibold tracking-tight">
            Usage
          </h2>
          <p className="text-sm text-muted-foreground">
            Billable call minutes by calendar month (UTC). A usage report, not an invoice.
          </p>
        </div>
        <select
          aria-label="Billing period"
          className="h-9 rounded-md border border-input bg-background px-2 text-sm"
          value={period}
          onChange={(event) => setPeriod(event.target.value)}
        >
          {periods.map((value) => (
            <option key={value} value={value}>
              {periodLabel(value)}
            </option>
          ))}
        </select>
      </div>

      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : !usage ? (
        <Skeleton className="h-24 w-full" />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard
            icon={Clock}
            label="Billable minutes"
            value={String(usage.billable_minutes)}
            hint="Each call rounded up to the minute"
          />
          <StatCard
            icon={PhoneCall}
            label="Billable calls"
            value={`${usage.billable_calls} / ${usage.calls}`}
            hint="Billable of all recorded calls"
          />
          <StatCard
            icon={PhoneOff}
            label="Not billed"
            value={String(usage.calls - usage.billable_calls)}
            hint={excluded || "No excluded calls"}
          />
          <StatCard
            icon={CheckCircle2}
            label="Checked against provider"
            value={`${reconciled} / ${usage.calls}`}
            hint={
              usage.unreconciled_calls > 0
                ? `${usage.unreconciled_calls} not yet reconciled`
                : "All calls reconciled"
            }
          />
        </div>
      )}
    </section>
  );
}
