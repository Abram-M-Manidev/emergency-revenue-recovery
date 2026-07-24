"use client";

import { useEffect, useState } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { createService, deleteService, fetchServices, updateService } from "@/lib/api/business-knowledge";
import { ApiError } from "@/lib/api/client";
import type { Service, ServicePayload } from "@/lib/api/types";
import { useToast } from "@/hooks/use-toast";

const EMPTY_FORM: ServicePayload = {
  name: "",
  description: "",
  category: "",
  is_emergency_eligible: false,
  is_active: true,
};

export function ServicesSection() {
  const { toast } = useToast();
  const [isLoading, setIsLoading] = useState(true);
  const [services, setServices] = useState<Service[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<ServicePayload>(EMPTY_FORM);
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchServices()
      .then((data) => {
        if (!cancelled) setServices(data);
      })
      .catch(() => toast({ title: "Failed to load services", variant: "destructive" }))
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function openCreate() {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setModalOpen(true);
  }

  function openEdit(service: Service) {
    setEditingId(service.id);
    setForm({
      name: service.name,
      description: service.description ?? "",
      category: service.category ?? "",
      is_emergency_eligible: service.is_emergency_eligible,
      is_active: service.is_active,
    });
    setModalOpen(true);
  }

  async function save() {
    setIsSaving(true);
    const payload: ServicePayload = {
      name: form.name,
      description: form.description || null,
      category: form.category || null,
      is_emergency_eligible: form.is_emergency_eligible,
      is_active: form.is_active,
    };
    try {
      if (editingId) {
        const updated = await updateService(editingId, payload);
        setServices((current) => current.map((s) => (s.id === editingId ? updated : s)));
      } else {
        const created = await createService(payload);
        setServices((current) => [...current, created]);
      }
      setModalOpen(false);
      toast({ title: editingId ? "Service updated" : "Service added", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to save service",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  }

  async function remove(id: string) {
    try {
      await deleteService(id);
      setServices((current) => current.filter((s) => s.id !== id));
    } catch {
      toast({ title: "Failed to remove service", variant: "destructive" });
    }
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="text-base">Services offered</CardTitle>
          <CardDescription>What this business does, and which are emergency-eligible.</CardDescription>
        </div>
        <Button variant="outline" size="sm" onClick={openCreate}>
          <Plus className="h-4 w-4" /> Add service
        </Button>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : services.length === 0 ? (
          <EmptyState title="No services yet" description="Add the services this business offers." />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Category</TableHead>
                <TableHead>Emergency</TableHead>
                <TableHead>Status</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {services.map((service) => (
                <TableRow key={service.id}>
                  <TableCell>{service.name}</TableCell>
                  <TableCell>{service.category ?? "—"}</TableCell>
                  <TableCell>
                    {service.is_emergency_eligible ? (
                      <Badge variant="destructive">Emergency</Badge>
                    ) : (
                      <Badge variant="outline">Standard</Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <Badge variant={service.is_active ? "success" : "secondary"}>
                      {service.is_active ? "Active" : "Inactive"}
                    </Badge>
                  </TableCell>
                  <TableCell className="flex gap-1">
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label="Edit service"
                      onClick={() => openEdit(service)}
                    >
                      <Pencil className="h-4 w-4" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label="Remove service"
                      onClick={() => remove(service.id)}
                    >
                      <Trash2 className="h-4 w-4" />
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
        title={editingId ? "Edit service" : "Add service"}
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
            <label className="text-sm font-medium">Name</label>
            <Input
              placeholder="Furnace repair"
              value={form.name}
              onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
            />
          </div>
          <div>
            <label className="text-sm font-medium">Description</label>
            <Input
              value={form.description ?? ""}
              onChange={(event) =>
                setForm((current) => ({ ...current, description: event.target.value }))
              }
            />
          </div>
          <div>
            <label className="text-sm font-medium">Category</label>
            <Input
              placeholder="Repair"
              value={form.category ?? ""}
              onChange={(event) => setForm((current) => ({ ...current, category: event.target.value }))}
            />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={form.is_emergency_eligible}
              onChange={(event) =>
                setForm((current) => ({ ...current, is_emergency_eligible: event.target.checked }))
              }
            />
            Emergency-eligible
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={form.is_active}
              onChange={(event) => setForm((current) => ({ ...current, is_active: event.target.checked }))}
            />
            Active
          </label>
        </div>
      </Modal>
    </Card>
  );
}
