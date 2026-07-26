"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import type { Customer, CustomerPayload } from "@/lib/api/types";

const EMPTY_FORM: CustomerPayload = {
  full_name: "",
  phone_number: "",
  email: "",
  address: "",
  notes: "",
};

function toForm(customer: Customer | null): CustomerPayload {
  if (!customer) return EMPTY_FORM;
  return {
    full_name: customer.full_name ?? "",
    phone_number: customer.phone_number,
    email: customer.email ?? "",
    address: customer.address ?? "",
    notes: customer.notes ?? "",
  };
}

interface CustomerFormModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `null` when creating a new customer, otherwise the customer being edited. */
  customer: Customer | null;
  onSubmit: (payload: CustomerPayload) => Promise<void>;
}

export function CustomerFormModal({ open, onOpenChange, customer, onSubmit }: CustomerFormModalProps) {
  const [form, setForm] = useState<CustomerPayload>(EMPTY_FORM);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setForm(toForm(customer));
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, customer]);

  async function handleSave() {
    if (!form.phone_number.trim()) {
      setError("Phone number is required.");
      return;
    }
    setIsSaving(true);
    setError(null);
    try {
      await onSubmit({
        full_name: form.full_name?.trim() || null,
        phone_number: form.phone_number.trim(),
        email: form.email?.trim() || null,
        address: form.address?.trim() || null,
        notes: form.notes?.trim() || null,
      });
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save customer.");
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={customer ? "Edit customer" : "Add customer"}
      footer={
        <>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave} isLoading={isSaving}>
            Save
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <label className="text-sm font-medium">Full name</label>
          <Input
            value={form.full_name ?? ""}
            onChange={(event) => setForm((current) => ({ ...current, full_name: event.target.value }))}
          />
        </div>
        <div>
          <label className="text-sm font-medium">Phone number</label>
          <Input
            value={form.phone_number}
            onChange={(event) =>
              setForm((current) => ({ ...current, phone_number: event.target.value }))
            }
          />
        </div>
        <div>
          <label className="text-sm font-medium">Email</label>
          <Input
            type="email"
            value={form.email ?? ""}
            onChange={(event) => setForm((current) => ({ ...current, email: event.target.value }))}
          />
        </div>
        <div>
          <label className="text-sm font-medium">Address</label>
          <Input
            value={form.address ?? ""}
            onChange={(event) => setForm((current) => ({ ...current, address: event.target.value }))}
          />
        </div>
        <div>
          <label className="text-sm font-medium">Notes</label>
          <Input
            value={form.notes ?? ""}
            onChange={(event) => setForm((current) => ({ ...current, notes: event.target.value }))}
          />
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
      </div>
    </Modal>
  );
}
