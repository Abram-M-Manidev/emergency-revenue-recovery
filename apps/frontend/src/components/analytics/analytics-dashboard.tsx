"use client";

import { useEffect, useState } from "react";
import { CalendarCheck, DollarSign, PhoneCall, Siren, UserPlus } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import { fetchAnalyticsSummary } from "@/lib/api/analytics";
import { ApiError } from "@/lib/api/client";
import type { AnalyticsSummary, DateRangePreset } from "@/lib/api/types";
import { useToast } from "@/hooks/use-toast";

import { BreakdownBarChart } from "./breakdown-bar-chart";
import { StatCard } from "./stat-card";
import { TrendChart } from "./trend-chart";

const RANGE_OPTIONS: { value: DateRangePreset; label: string }[] = [
  { value: "today", label: "Today" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "90d", label: "Last 90 days" },
  { value: "all", label: "All time" },
];

const currencyFormatter = new Intl.NumberFormat(undefined, {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

export function AnalyticsDashboard() {
  const { toast } = useToast();
  const [range, setRange] = useState<DateRangePreset>("30d");
  const [summary, setSummary] = useState<AnalyticsSummary | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    fetchAnalyticsSummary(range)
      .then((data) => {
        if (!cancelled) setSummary(data);
      })
      .catch((error) => {
        if (cancelled) return;
        toast({
          title: error instanceof ApiError ? error.message : "Failed to load analytics",
          variant: "destructive",
        });
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Analytics</h1>
          <p className="text-sm text-muted-foreground">
            Call volume, conversion funnel, and revenue recovered.
          </p>
        </div>
        <select
          className="h-9 rounded-md border border-input bg-background px-2 text-sm"
          value={range}
          onChange={(event) => setRange(event.target.value as DateRangePreset)}
        >
          {RANGE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      {isLoading || !summary ? (
        <Skeleton className="h-64 w-full" />
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard
              icon={PhoneCall}
              label="Calls"
              value={String(summary.total_conversations)}
              hint="Total conversations in range"
            />
            <StatCard
              icon={Siren}
              label="Tickets resolved"
              value={`${summary.tickets_resolved} / ${summary.tickets_created}`}
              hint={
                summary.average_ticket_resolution_minutes !== null
                  ? `Avg. ${Math.round(summary.average_ticket_resolution_minutes)} min to resolve`
                  : undefined
              }
            />
            <StatCard
              icon={CalendarCheck}
              label="Appointments completed"
              value={`${summary.appointments_completed} / ${summary.appointments_created}`}
              hint={
                summary.appointment_show_up_rate !== null
                  ? `${Math.round(summary.appointment_show_up_rate * 100)}% show-up rate`
                  : undefined
              }
            />
            <StatCard
              icon={UserPlus}
              label="New customers"
              value={String(summary.new_customers)}
              hint={`${summary.total_customers} total`}
            />
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <StatCard
              icon={DollarSign}
              label="Revenue recovered"
              value={currencyFormatter.format(summary.total_revenue)}
              hint={`${currencyFormatter.format(summary.ticket_revenue)} tickets · ${currencyFormatter.format(summary.appointment_revenue)} appointments`}
              className="sm:col-span-2"
            />
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <TrendChart
              title="Calls per day"
              data={summary.conversations_by_day.map((entry) => ({
                day: entry.day,
                value: entry.count,
              }))}
            />
            <TrendChart
              title="Revenue recovered per day"
              data={summary.revenue_by_day.map((entry) => ({ day: entry.day, value: entry.amount }))}
              color="hsl(var(--success))"
              valueFormatter={(value) => currencyFormatter.format(value)}
            />
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <BreakdownBarChart
              title="Call classification"
              description="Emergency vs. routine calls"
              data={summary.classification_breakdown}
            />
            <BreakdownBarChart
              title="Recommended actions"
              description="What the AI Brain decided to do"
              data={summary.recommended_action_breakdown}
            />
            <BreakdownBarChart
              title="Appointment outcomes"
              description="Status of appointments created in range"
              data={summary.appointment_status_breakdown}
            />
          </div>
        </>
      )}
    </div>
  );
}
