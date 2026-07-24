"use client";

import { useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  createServiceArea,
  deleteServiceArea,
  fetchServiceAreas,
} from "@/lib/api/business-knowledge";
import { ApiError } from "@/lib/api/client";
import type { ServiceArea } from "@/lib/api/types";
import { useToast } from "@/hooks/use-toast";

const EMPTY_FORM = { label: "", postal_code: "", city: "", state: "" };

export function ServiceAreasSection() {
  const { toast } = useToast();
  const [isLoading, setIsLoading] = useState(true);
  const [areas, setAreas] = useState<ServiceArea[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchServiceAreas()
      .then((data) => {
        if (!cancelled) setAreas(data);
      })
      .catch(() => toast({ title: "Failed to load service areas", variant: "destructive" }))
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function addArea() {
    setIsSaving(true);
    try {
      const created = await createServiceArea({
        label: form.label,
        postal_code: form.postal_code || null,
        city: form.city || null,
        state: form.state || null,
      });
      setAreas((current) => [...current, created]);
      setModalOpen(false);
      setForm(EMPTY_FORM);
      toast({ title: "Service area added", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to add service area",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  }

  async function removeArea(id: string) {
    try {
      await deleteServiceArea(id);
      setAreas((current) => current.filter((area) => area.id !== id));
    } catch {
      toast({ title: "Failed to remove service area", variant: "destructive" });
    }
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="text-base">Service areas</CardTitle>
          <CardDescription>Cities or zip codes this business serves.</CardDescription>
        </div>
        <Button variant="outline" size="sm" onClick={() => setModalOpen(true)}>
          <Plus className="h-4 w-4" /> Add area
        </Button>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : areas.length === 0 ? (
          <EmptyState title="No service areas yet" description="Add the cities or zip codes you serve." />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Label</TableHead>
                <TableHead>City</TableHead>
                <TableHead>State</TableHead>
                <TableHead>Postal code</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {areas.map((area) => (
                <TableRow key={area.id}>
                  <TableCell>{area.label}</TableCell>
                  <TableCell>{area.city ?? "—"}</TableCell>
                  <TableCell>{area.state ?? "—"}</TableCell>
                  <TableCell>{area.postal_code ?? "—"}</TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label="Remove service area"
                      onClick={() => removeArea(area.id)}
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
        title="Add service area"
        footer={
          <>
            <Button variant="outline" onClick={() => setModalOpen(false)}>
              Cancel
            </Button>
            <Button onClick={addArea} isLoading={isSaving}>
              Add
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="text-sm font-medium">Label</label>
            <Input
              placeholder="Downtown Metro"
              value={form.label}
              onChange={(event) => setForm((current) => ({ ...current, label: event.target.value }))}
            />
          </div>
          <div>
            <label className="text-sm font-medium">City</label>
            <Input
              value={form.city}
              onChange={(event) => setForm((current) => ({ ...current, city: event.target.value }))}
            />
          </div>
          <div>
            <label className="text-sm font-medium">State</label>
            <Input
              value={form.state}
              onChange={(event) => setForm((current) => ({ ...current, state: event.target.value }))}
            />
          </div>
          <div>
            <label className="text-sm font-medium">Postal code</label>
            <Input
              value={form.postal_code}
              onChange={(event) =>
                setForm((current) => ({ ...current, postal_code: event.target.value }))
              }
            />
          </div>
        </div>
      </Modal>
    </Card>
  );
}
