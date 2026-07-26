"use client";

import { CustomerList } from "@/components/customers/customer-list";

export default function CustomersPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Customers</h1>
        <p className="text-sm text-muted-foreground">
          A unified record per caller, linking their emergency tickets and appointments.
        </p>
      </div>

      <CustomerList />
    </div>
  );
}
