"use client";

import { Activity, PhoneCall, Siren } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { useAuth } from "@/hooks/use-auth";

const UPCOMING_MODULES = [
  {
    icon: PhoneCall,
    title: "Call activity",
    description: "After-hours call volume and outcomes will appear here.",
  },
  {
    icon: Siren,
    title: "Emergency tickets",
    description: "Dispatched emergencies will be tracked here.",
  },
  {
    icon: Activity,
    title: "Revenue recovered",
    description: "Recovered revenue analytics will appear here.",
  },
];

export default function DashboardPage() {
  const { user } = useAuth();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-sm text-muted-foreground">
          {user ? `Signed in as ${user.email}` : "Loading your workspace..."}
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        {UPCOMING_MODULES.map((module) => (
          <Card key={module.title}>
            <CardHeader>
              <CardTitle className="text-base">{module.title}</CardTitle>
            </CardHeader>
            <CardContent>
              <EmptyState icon={module.icon} title="Coming soon" description={module.description} />
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
