"use client";

import { useEffect, useState } from "react";
import { PhoneCall, Siren, TrendingUp, Users } from "lucide-react";
import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { useAuth } from "@/hooks/use-auth";
import { fetchAnalyticsSummary } from "@/lib/api/analytics";
import { fetchCustomers } from "@/lib/api/customers";
import { fetchTickets } from "@/lib/api/dispatch";
import type { AnalyticsSummary } from "@/lib/api/types";
import { ROUTES } from "@/lib/constants";

const currencyFormatter = new Intl.NumberFormat(undefined, {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

function useAnalyticsOverview() {
  const [summary, setSummary] = useState<AnalyticsSummary | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchAnalyticsSummary("30d")
      .then((data) => {
        if (!cancelled) setSummary(data);
      })
      .catch(() => {
        if (!cancelled) setSummary(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return summary;
}

function useOpenTicketCount() {
  const [count, setCount] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchTickets("new"), fetchTickets("assigned"), fetchTickets("en_route")])
      .then(([created, assigned, enRoute]) => {
        if (!cancelled) setCount(created.length + assigned.length + enRoute.length);
      })
      .catch(() => {
        if (!cancelled) setCount(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return count;
}

function useCustomerCount() {
  const [count, setCount] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchCustomers()
      .then((customers) => {
        if (!cancelled) setCount(customers.length);
      })
      .catch(() => {
        if (!cancelled) setCount(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return count;
}

export default function DashboardPage() {
  const { user } = useAuth();
  const openTicketCount = useOpenTicketCount();
  const customerCount = useCustomerCount();
  const analytics = useAnalyticsOverview();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-sm text-muted-foreground">
          {user ? `Signed in as ${user.email}` : "Loading your workspace..."}
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Link href={ROUTES.dispatch}>
          <Card className="h-full transition-colors hover:border-primary/50">
            <CardHeader>
              <CardTitle className="text-base">Emergency tickets</CardTitle>
            </CardHeader>
            <CardContent>
              {openTicketCount === null ? (
                <EmptyState
                  icon={Siren}
                  title="—"
                  description="Dispatched emergencies are tracked here."
                />
              ) : (
                <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border p-12 text-center">
                  <Siren className="h-6 w-6 text-muted-foreground" aria-hidden="true" />
                  <p className="text-2xl font-semibold">{openTicketCount}</p>
                  <p className="text-sm text-muted-foreground">open ticket{openTicketCount === 1 ? "" : "s"}</p>
                </div>
              )}
            </CardContent>
          </Card>
        </Link>
        <Link href={ROUTES.customers}>
          <Card className="h-full transition-colors hover:border-primary/50">
            <CardHeader>
              <CardTitle className="text-base">Customers</CardTitle>
            </CardHeader>
            <CardContent>
              {customerCount === null ? (
                <EmptyState
                  icon={Users}
                  title="—"
                  description="Unified customer records are tracked here."
                />
              ) : (
                <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border p-12 text-center">
                  <Users className="h-6 w-6 text-muted-foreground" aria-hidden="true" />
                  <p className="text-2xl font-semibold">{customerCount}</p>
                  <p className="text-sm text-muted-foreground">
                    customer{customerCount === 1 ? "" : "s"}
                  </p>
                </div>
              )}
            </CardContent>
          </Card>
        </Link>
        <Link href={ROUTES.analytics}>
          <Card className="h-full transition-colors hover:border-primary/50">
            <CardHeader>
              <CardTitle className="text-base">Call activity</CardTitle>
            </CardHeader>
            <CardContent>
              {analytics === null ? (
                <EmptyState
                  icon={PhoneCall}
                  title="—"
                  description="After-hours call volume will appear here."
                />
              ) : (
                <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border p-12 text-center">
                  <PhoneCall className="h-6 w-6 text-muted-foreground" aria-hidden="true" />
                  <p className="text-2xl font-semibold">{analytics.total_conversations}</p>
                  <p className="text-sm text-muted-foreground">calls in the last 30 days</p>
                </div>
              )}
            </CardContent>
          </Card>
        </Link>
        <Link href={ROUTES.analytics}>
          <Card className="h-full transition-colors hover:border-primary/50">
            <CardHeader>
              <CardTitle className="text-base">Revenue recovered</CardTitle>
            </CardHeader>
            <CardContent>
              {analytics === null ? (
                <EmptyState
                  icon={TrendingUp}
                  title="—"
                  description="Recovered revenue analytics will appear here."
                />
              ) : (
                <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border p-12 text-center">
                  <TrendingUp className="h-6 w-6 text-muted-foreground" aria-hidden="true" />
                  <p className="text-2xl font-semibold">
                    {currencyFormatter.format(analytics.total_revenue)}
                  </p>
                  <p className="text-sm text-muted-foreground">in the last 30 days</p>
                </div>
              )}
            </CardContent>
          </Card>
        </Link>
      </div>
    </div>
  );
}
