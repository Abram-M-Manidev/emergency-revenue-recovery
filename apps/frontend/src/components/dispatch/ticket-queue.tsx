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
import { acknowledgeTicketPage, fetchRecentPages } from "@/lib/api/paging";
import type {
  EmergencyPage,
  EmergencyTicket,
  PageStatus,
  TechnicianProfile,
  TicketStatus,
} from "@/lib/api/types";
import { useAuth } from "@/hooks/use-auth";
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

/**
 * What each page state honestly means. "Paged" is only ever shown once a
 * provider accepted a page, and never implies anyone read it.
 */
function pagingLabel(page: EmergencyPage): {
  label: string;
  variant: "destructive" | "default" | "success" | "secondary";
} {
  if (page.status === "acknowledged") return { label: "Acknowledged", variant: "success" };
  if (page.status === "unresolved") return { label: "Unresolved — nobody acknowledged", variant: "destructive" };
  const sent = page.notifications.some((n) => n.status === "sent");
  const who = page.status === "paging_backup" ? "backup" : "primary";
  return sent
    ? { label: `Paged ${who} — awaiting ack`, variant: "default" }
    : { label: `Paging ${who}…`, variant: "secondary" };
}

const ACKNOWLEDGEABLE: PageStatus[] = ["paging_primary", "paging_backup", "unresolved"];

export function TicketQueue() {
  const { toast } = useToast();
  const { user } = useAuth();
  const canAcknowledge =
    (user?.permissions.includes("dispatch:manage") ||
      user?.permissions.includes("dispatch:update_assigned")) ??
    false;
  const [pages, setPages] = useState<Record<string, EmergencyPage>>({});
  const [isLoading, setIsLoading] = useState(true);
  const [tickets, setTickets] = useState<EmergencyTicket[]>([]);
  const [technicians, setTechnicians] = useState<TechnicianProfile[]>([]);
  const [statusFilter, setStatusFilter] = useState<TicketStatus | "all">("all");
  const [selectedTechnician, setSelectedTechnician] = useState<Record<string, string>>({});
  const [resolvedValue, setResolvedValue] = useState<Record<string, string>>({});

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
      // Paging is supplementary: a failure here must not hide the queue.
      fetchRecentPages().catch(() => [] as EmergencyPage[]),
    ])
      .then(([ticketData, technicianData, pageData]) => {
        if (cancelled) return;
        setTickets(ticketData);
        setTechnicians(technicianData);
        setPages(Object.fromEntries(pageData.map((page) => [page.ticket_id, page])));
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

  async function handleAcknowledge(ticket: EmergencyTicket) {
    try {
      const result = await acknowledgeTicketPage(ticket.id);
      setPages((current) => {
        const page = current[ticket.id];
        if (!page) return current;
        return {
          ...current,
          [ticket.id]: { ...page, status: result.status, acknowledged_at: result.acknowledged_at },
        };
      });
      toast({
        title: result.already_acknowledged ? "Already acknowledged" : "Emergency acknowledged — escalation stopped",
        variant: "success",
      });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to acknowledge",
        variant: "destructive",
      });
    }
  }

  async function handleStatusChange(
    ticket: EmergencyTicket,
    status: TicketStatus,
    actualValue?: number,
  ) {
    try {
      const updated = await updateTicketStatus(ticket.id, status, actualValue);
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
                <TableHead>On-call paging</TableHead>
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
                const page = pages[ticket.id];
                const paging = page ? pagingLabel(page) : null;
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
                    <TableCell>
                      {page && paging ? (
                        <div className="flex flex-col items-start gap-1">
                          <Badge variant={paging.variant}>{paging.label}</Badge>
                          {canAcknowledge && ACKNOWLEDGEABLE.includes(page.status) ? (
                            <Button size="sm" variant="outline" onClick={() => handleAcknowledge(ticket)}>
                              Acknowledge
                            </Button>
                          ) : null}
                        </div>
                      ) : (
                        <span className="text-xs text-muted-foreground">Not paged</span>
                      )}
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
                        <div className="flex items-center gap-2">
                          <input
                            type="number"
                            min={0}
                            step="0.01"
                            placeholder="Value ($)"
                            className="h-8 w-24 rounded-md border border-input bg-background px-2 text-xs"
                            value={resolvedValue[ticket.id] ?? ""}
                            onChange={(event) =>
                              setResolvedValue((current) => ({
                                ...current,
                                [ticket.id]: event.target.value,
                              }))
                            }
                          />
                          <Button
                            size="sm"
                            onClick={() =>
                              handleStatusChange(
                                ticket,
                                "resolved",
                                resolvedValue[ticket.id] ? Number(resolvedValue[ticket.id]) : undefined,
                              )
                            }
                          >
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
