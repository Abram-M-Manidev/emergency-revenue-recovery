"use client";

import { useEffect, useState } from "react";
import { Plus } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  createTechnician,
  fetchTechnicians,
  setTechnicianOnCall,
} from "@/lib/api/dispatch";
import { ApiError } from "@/lib/api/client";
import type { CreateTechnicianPayload, TechnicianProfile } from "@/lib/api/types";
import { useToast } from "@/hooks/use-toast";

const EMPTY_FORM: CreateTechnicianPayload = {
  full_name: "",
  email: "",
  phone_number: "",
  temporary_password: "",
};

export function TechnicianRoster() {
  const { toast } = useToast();
  const [isLoading, setIsLoading] = useState(true);
  const [technicians, setTechnicians] = useState<TechnicianProfile[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [form, setForm] = useState<CreateTechnicianPayload>(EMPTY_FORM);
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchTechnicians()
      .then((data) => {
        if (!cancelled) setTechnicians(data);
      })
      .catch(() => toast({ title: "Failed to load technicians", variant: "destructive" }))
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function openCreate() {
    setForm(EMPTY_FORM);
    setModalOpen(true);
  }

  async function save() {
    setIsSaving(true);
    try {
      const created = await createTechnician(form);
      setTechnicians((current) => [...current, created]);
      setModalOpen(false);
      toast({ title: "Technician added", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to add technician",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  }

  async function toggleOnCall(technician: TechnicianProfile) {
    try {
      const updated = await setTechnicianOnCall(technician.user_id, !technician.is_on_call);
      setTechnicians((current) =>
        current.map((t) =>
          t.user_id === technician.user_id ? { ...t, is_on_call: updated.is_on_call } : t,
        ),
      );
    } catch {
      toast({ title: "Failed to update on-call status", variant: "destructive" });
    }
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="text-base">Technician roster</CardTitle>
          <CardDescription>Who can be assigned an emergency ticket.</CardDescription>
        </div>
        <Button variant="outline" size="sm" onClick={openCreate}>
          <Plus className="h-4 w-4" /> Add technician
        </Button>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : technicians.length === 0 ? (
          <EmptyState
            title="No technicians yet"
            description="Add a technician so tickets can be assigned to them."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Email</TableHead>
                <TableHead>Phone</TableHead>
                <TableHead>On call</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {technicians.map((technician) => (
                <TableRow key={technician.id}>
                  <TableCell>{technician.full_name ?? "—"}</TableCell>
                  <TableCell>{technician.email ?? "—"}</TableCell>
                  <TableCell>{technician.phone_number}</TableCell>
                  <TableCell>
                    <Badge variant={technician.is_on_call ? "success" : "secondary"}>
                      {technician.is_on_call ? "On call" : "Off"}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Button variant="ghost" size="sm" onClick={() => toggleOnCall(technician)}>
                      {technician.is_on_call ? "Set off call" : "Set on call"}
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>

      <Modal
        open={modalOpen}
        onOpenChange={setModalOpen}
        title="Add technician"
        description="Creates a login for this technician with the Technician role."
        footer={
          <>
            <Button variant="outline" onClick={() => setModalOpen(false)}>
              Cancel
            </Button>
            <Button onClick={save} isLoading={isSaving}>
              Save
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="text-sm font-medium">Full name</label>
            <Input
              value={form.full_name}
              onChange={(event) => setForm((current) => ({ ...current, full_name: event.target.value }))}
            />
          </div>
          <div>
            <label className="text-sm font-medium">Email</label>
            <Input
              type="email"
              value={form.email}
              onChange={(event) => setForm((current) => ({ ...current, email: event.target.value }))}
            />
          </div>
          <div>
            <label className="text-sm font-medium">Phone number</label>
            <Input
              placeholder="+15551234567"
              value={form.phone_number}
              onChange={(event) =>
                setForm((current) => ({ ...current, phone_number: event.target.value }))
              }
            />
          </div>
          <div>
            <label className="text-sm font-medium">Temporary password</label>
            <Input
              type="password"
              minLength={8}
              value={form.temporary_password}
              onChange={(event) =>
                setForm((current) => ({ ...current, temporary_password: event.target.value }))
              }
            />
          </div>
        </div>
      </Modal>
    </Card>
  );
}
