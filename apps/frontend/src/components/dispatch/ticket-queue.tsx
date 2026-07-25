"use client";

import { useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  assignTicket,
  fetchTechnicians,
  fetchTickets,
  updateTicketStatus,
} from "@/lib/api/dispatch";
import { ApiError } from "@/lib/api/client";
import type { EmergencyTicket, TechnicianProfile, TicketStatus } from "@/lib/api/types";
import { useToast } from "@/hooks/use-toast";

const STATUS_FILTERS: { value: TicketStatus | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "new", label: "New" },
  { value: "assigned", label: "Assigned" },
  { value: "en_route", label: "En route" },
  { value: "resolved", label: "Resolved" },
  { value: "canceled", label: "Canceled" },
];

const STATUS_BADGE_VARIANT: Record<TicketStatus, "destructive" | "default" | "success" | "secondary"> = {
  new: "destructive",
  assigned: "default",
  en_route: "default",
  resolved: "success",
  canceled: "secondary",
};

const STATUS_LABEL: Record<TicketStatus, string> = {
  new: "New",
  assigned: "Assigned",
  en_route: "En route",
  resolved: "Resolved",
  canceled: "Canceled",
};

export function TicketQueue() {
  const { toast } = useToast();
  const [isLoading, setIsLoading] = useState(true);
  const [tickets, setTickets] = useState<EmergencyTicket[]>([]);
  const [technicians, setTechnicians] = useState<TechnicianProfile[]>([]);
  const [statusFilter, setStatusFilter] = useState<TicketStatus | "all">("all");
  const [selectedTechnician, setSelectedTechnician] = useState<Record<string, string>>({});

  const technicianById = useMemo(
    () => new Map(technicians.map((t) => [t.user_id, t])),
    [technicians],
  );

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    Promise.all([
      fetchTickets(statusFilter === "all" ? undefined : statusFilter),
      fetchTechnicians(),
    ])
      .then(([ticketData, technicianData]) => {
        if (cancelled) return;
        setTickets(ticketData);
        setTechnicians(technicianData);
      })
      .catch(() => toast({ title: "Failed to load dispatch queue", variant: "destructive" }))
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter]);

  function replaceTicket(updated: EmergencyTicket) {
    setTickets((current) => current.map((t) => (t.id === updated.id ? updated : t)));
  }

  async function handleAssign(ticket: EmergencyTicket) {
    const technicianUserId = selectedTechnician[ticket.id];
    if (!technicianUserId) {
      toast({ title: "Choose a technician first", variant: "destructive" });
      return;
    }
    try {
      const updated = await assignTicket(ticket.id, technicianUserId);
      replaceTicket(updated);
      toast({ title: "Ticket assigned", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to assign ticket",
        variant: "destructive",
      });
    }
  }

  async function handleStatusChange(ticket: EmergencyTicket, status: TicketStatus) {
    try {
      const updated = await updateTicketStatus(ticket.id, status);
      replaceTicket(updated);
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to update ticket",
        variant: "destructive",
      });
    }
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="text-base">Emergency tickets</CardTitle>
          <CardDescription>Created automatically when the AI Brain flags a call as an emergency.</CardDescription>
        </div>
        <select
          className="h-9 rounded-md border border-input bg-background px-2 text-sm"
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value as TicketStatus | "all")}
        >
          {STATUS_FILTERS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : tickets.length === 0 ? (
          <EmptyState
            title="No tickets"
            description="Emergency tickets created from AI Brain calls will appear here."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Customer</TableHead>
                <TableHead>Summary</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Assigned to</TableHead>
                <TableHead>Created</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {tickets.map((ticket) => {
                const assignedTechnician = ticket.assigned_technician_user_id
                  ? technicianById.get(ticket.assigned_technician_user_id)
                  : undefined;
                return (
                  <TableRow key={ticket.id}>
                    <TableCell>
                      <div className="font-medium">{ticket.customer_name ?? "Unknown caller"}</div>
                      <div className="text-xs text-muted-foreground">
                        {ticket.customer_phone ?? "—"}
                        {ticket.customer_address ? ` · ${ticket.customer_address}` : ""}
                      </div>
                    </TableCell>
                    <TableCell className="max-w-xs truncate" title={ticket.summary}>
                      {ticket.summary}
                    </TableCell>
                    <TableCell>
                      <Badge variant={STATUS_BADGE_VARIANT[ticket.status]}>
                        {STATUS_LABEL[ticket.status]}
                      </Badge>
                    </TableCell>
                    <TableCell>{assignedTechnician?.full_name ?? "—"}</TableCell>
                    <TableCell>{new Date(ticket.created_at).toLocaleString()}</TableCell>
                    <TableCell>
                      {ticket.status === "new" ? (
                        <div className="flex items-center gap-2">
                          <select
                            className="h-8 rounded-md border border-input bg-background px-2 text-xs"
                            value={selectedTechnician[ticket.id] ?? ""}
                            onChange={(event) =>
                              setSelectedTechnician((current) => ({
                                ...current,
                                [ticket.id]: event.target.value,
                              }))
                            }
                          >
                            <option value="">Choose technician…</option>
                            {technicians.map((technician) => (
                              <option key={technician.user_id} value={technician.user_id}>
                                {technician.full_name ?? technician.phone_number}
                              </option>
                            ))}
                          </select>
                          <Button size="sm" onClick={() => handleAssign(ticket)}>
                            Assign
                          </Button>
                        </div>
                      ) : ticket.status === "assigned" ? (
                        <div className="flex gap-2">
                          <Button size="sm" onClick={() => handleStatusChange(ticket, "en_route")}>
                            Mark en route
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => handleStatusChange(ticket, "canceled")}
                          >
                            Cancel
                          </Button>
                        </div>
                      ) : ticket.status === "en_route" ? (
                        <div className="flex gap-2">
                          <Button size="sm" onClick={() => handleStatusChange(ticket, "resolved")}>
                            Mark resolved
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => handleStatusChange(ticket, "canceled")}
                          >
                            Cancel
                          </Button>
                        </div>
                      ) : null}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
