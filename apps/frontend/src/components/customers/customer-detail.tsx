"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fetchCustomerHistory, updateCustomer } from "@/lib/api/customers";
import { ApiError } from "@/lib/api/client";
import type {
  AppointmentStatus,
  CustomerHistory,
  CustomerPayload,
  TicketStatus,
} from "@/lib/api/types";
import { useToast } from "@/hooks/use-toast";
import { ROUTES } from "@/lib/constants";

import { CustomerFormModal } from "./customer-form-modal";

const TICKET_STATUS_LABEL: Record<TicketStatus, string> = {
  new: "New",
  assigned: "Assigned",
  en_route: "En route",
  resolved: "Resolved",
  canceled: "Canceled",
};

const APPOINTMENT_STATUS_LABEL: Record<AppointmentStatus, string> = {
  requested: "Requested",
  scheduled: "Scheduled",
  completed: "Completed",
  canceled: "Canceled",
  no_show: "No-show",
};

interface CustomerDetailProps {
  customerId: string;
}

export function CustomerDetail({ customerId }: CustomerDetailProps) {
  const { toast } = useToast();
  const router = useRouter();
  const [isLoading, setIsLoading] = useState(true);
  const [history, setHistory] = useState<CustomerHistory | null>(null);
  const [isEditOpen, setIsEditOpen] = useState(false);
  const [notFound, setNotFound] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    fetchCustomerHistory(customerId)
      .then((data) => {
        if (!cancelled) setHistory(data);
      })
      .catch((error) => {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 404) {
          setNotFound(true);
        } else {
          toast({ title: "Failed to load customer", variant: "destructive" });
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [customerId]);

  async function handleUpdate(payload: CustomerPayload) {
    const updated = await updateCustomer(customerId, payload);
    setHistory((current) => (current ? { ...current, customer: updated } : current));
    toast({ title: "Customer updated", variant: "success" });
  }

  if (isLoading) {
    return <Skeleton className="h-48 w-full" />;
  }

  if (notFound || !history) {
    return (
      <EmptyState
        title="Customer not found"
        description="This customer doesn't exist or belongs to a different organization."
      />
    );
  }

  const { customer, tickets, appointments } = history;

  return (
    <div className="space-y-6">
      <Button variant="outline" size="sm" onClick={() => router.push(ROUTES.customers)}>
        ← Back to customers
      </Button>

      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <div>
            <CardTitle className="text-xl">{customer.full_name ?? "Unknown caller"}</CardTitle>
            <p className="text-sm text-muted-foreground">{customer.phone_number}</p>
          </div>
          <Button variant="outline" onClick={() => setIsEditOpen(true)}>
            Edit
          </Button>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-3 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-muted-foreground">Email</dt>
              <dd>{customer.email ?? "—"}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Address</dt>
              <dd>{customer.address ?? "—"}</dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="text-muted-foreground">Notes</dt>
              <dd>{customer.notes ?? "—"}</dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Emergency tickets</CardTitle>
        </CardHeader>
        <CardContent>
          {tickets.length === 0 ? (
            <EmptyState title="No tickets" description="This customer has no emergency tickets." />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Summary</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {tickets.map((ticket) => (
                  <TableRow key={ticket.id}>
                    <TableCell className="max-w-xs truncate" title={ticket.summary}>
                      {ticket.summary}
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">{TICKET_STATUS_LABEL[ticket.status]}</Badge>
                    </TableCell>
                    <TableCell>{new Date(ticket.created_at).toLocaleString()}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {tickets.length > 0 ? (
            <div className="mt-3">
              <Link href={ROUTES.dispatch} className="text-sm text-primary hover:underline">
                View in Dispatch →
              </Link>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Appointments</CardTitle>
        </CardHeader>
        <CardContent>
          {appointments.length === 0 ? (
            <EmptyState
              title="No appointments"
              description="This customer has no appointments."
            />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Summary</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Scheduled for</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {appointments.map((appointment) => (
                  <TableRow key={appointment.id}>
                    <TableCell className="max-w-xs truncate" title={appointment.summary}>
                      {appointment.summary}
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">
                        {APPOINTMENT_STATUS_LABEL[appointment.status]}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {appointment.scheduled_start_at
                        ? new Date(appointment.scheduled_start_at).toLocaleString()
                        : "—"}
                    </TableCell>
                    <TableCell>{new Date(appointment.created_at).toLocaleString()}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {appointments.length > 0 ? (
            <div className="mt-3">
              <Link href={ROUTES.appointments} className="text-sm text-primary hover:underline">
                View in Appointments →
              </Link>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <CustomerFormModal
        open={isEditOpen}
        onOpenChange={setIsEditOpen}
        customer={customer}
        onSubmit={async (payload) => {
          try {
            await handleUpdate(payload);
          } catch (error) {
            throw new Error(
              error instanceof ApiError ? error.message : "Failed to update customer.",
            );
          }
        }}
      />
    </div>
  );
}
