"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { createCustomer, fetchCustomers } from "@/lib/api/customers";
import { ApiError } from "@/lib/api/client";
import type { Customer, CustomerPayload } from "@/lib/api/types";
import { useToast } from "@/hooks/use-toast";
import { ROUTES } from "@/lib/constants";

import { CustomerFormModal } from "./customer-form-modal";

export function CustomerList() {
  const { toast } = useToast();
  const [isLoading, setIsLoading] = useState(true);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [search, setSearch] = useState("");
  const [isAddOpen, setIsAddOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    const handle = setTimeout(() => {
      fetchCustomers(search || undefined)
        .then((data) => {
          if (!cancelled) setCustomers(data);
        })
        .catch(() => toast({ title: "Failed to load customers", variant: "destructive" }))
        .finally(() => {
          if (!cancelled) setIsLoading(false);
        });
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  async function handleCreate(payload: CustomerPayload) {
    const created = await createCustomer(payload);
    setCustomers((current) => [created, ...current]);
    toast({ title: "Customer added", variant: "success" });
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="text-base">Customers</CardTitle>
          <CardDescription>
            Unified caller records, populated automatically from AI Brain calls.
          </CardDescription>
        </div>
        <div className="flex items-center gap-2">
          <Input
            placeholder="Search name or phone…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            className="w-56"
          />
          <Button onClick={() => setIsAddOpen(true)}>Add customer</Button>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : customers.length === 0 ? (
          <EmptyState
            title="No customers"
            description="Customers captured from AI Brain calls, or added by hand, will appear here."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Phone</TableHead>
                <TableHead>Email</TableHead>
                <TableHead>Added</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {customers.map((customer) => (
                <TableRow key={customer.id}>
                  <TableCell>
                    <Link
                      href={`${ROUTES.customers}/${customer.id}`}
                      className="font-medium text-primary hover:underline"
                    >
                      {customer.full_name ?? "Unknown caller"}
                    </Link>
                  </TableCell>
                  <TableCell>{customer.phone_number}</TableCell>
                  <TableCell>{customer.email ?? "—"}</TableCell>
                  <TableCell>{new Date(customer.created_at).toLocaleDateString()}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>

      <CustomerFormModal
        open={isAddOpen}
        onOpenChange={setIsAddOpen}
        customer={null}
        onSubmit={async (payload) => {
          try {
            await handleCreate(payload);
          } catch (error) {
            throw new Error(error instanceof ApiError ? error.message : "Failed to add customer.");
          }
        }}
      />
    </Card>
  );
}
