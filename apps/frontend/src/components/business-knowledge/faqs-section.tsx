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
import { createFAQ, deleteFAQ, fetchFAQs, updateFAQ } from "@/lib/api/business-knowledge";
import { ApiError } from "@/lib/api/client";
import type { FAQEntry, FAQPayload } from "@/lib/api/types";
import { useToast } from "@/hooks/use-toast";

const EMPTY_FORM: FAQPayload = { question: "", answer: "", category: "", is_active: true };

export function FAQsSection() {
  const { toast } = useToast();
  const [isLoading, setIsLoading] = useState(true);
  const [faqs, setFaqs] = useState<FAQEntry[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<FAQPayload>(EMPTY_FORM);
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchFAQs()
      .then((data) => {
        if (!cancelled) setFaqs(data);
      })
      .catch(() => toast({ title: "Failed to load FAQs", variant: "destructive" }))
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

  function openEdit(faq: FAQEntry) {
    setEditingId(faq.id);
    setForm({
      question: faq.question,
      answer: faq.answer,
      category: faq.category ?? "",
      is_active: faq.is_active,
    });
    setModalOpen(true);
  }

  async function save() {
    setIsSaving(true);
    const payload: FAQPayload = {
      question: form.question,
      answer: form.answer,
      category: form.category || null,
      is_active: form.is_active,
    };
    try {
      if (editingId) {
        const updated = await updateFAQ(editingId, payload);
        setFaqs((current) => current.map((f) => (f.id === editingId ? updated : f)));
      } else {
        const created = await createFAQ(payload);
        setFaqs((current) => [...current, created]);
      }
      setModalOpen(false);
      toast({ title: editingId ? "FAQ updated" : "FAQ added", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to save FAQ",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  }

  async function remove(id: string) {
    try {
      await deleteFAQ(id);
      setFaqs((current) => current.filter((f) => f.id !== id));
    } catch {
      toast({ title: "Failed to remove FAQ", variant: "destructive" });
    }
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="text-base">FAQs</CardTitle>
          <CardDescription>Answers the AI will use for non-emergency questions.</CardDescription>
        </div>
        <Button variant="outline" size="sm" onClick={openCreate}>
          <Plus className="h-4 w-4" /> Add FAQ
        </Button>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : faqs.length === 0 ? (
          <EmptyState title="No FAQs yet" description="Add common questions and their answers." />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Question</TableHead>
                <TableHead>Category</TableHead>
                <TableHead>Status</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {faqs.map((faq) => (
                <TableRow key={faq.id}>
                  <TableCell>{faq.question}</TableCell>
                  <TableCell>{faq.category ?? "—"}</TableCell>
                  <TableCell>
                    <Badge variant={faq.is_active ? "success" : "secondary"}>
                      {faq.is_active ? "Active" : "Inactive"}
                    </Badge>
                  </TableCell>
                  <TableCell className="flex gap-1">
                    <Button variant="ghost" size="icon" aria-label="Edit FAQ" onClick={() => openEdit(faq)}>
                      <Pencil className="h-4 w-4" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label="Remove FAQ"
                      onClick={() => remove(faq.id)}
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
        title={editingId ? "Edit FAQ" : "Add FAQ"}
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
            <label className="text-sm font-medium">Question</label>
            <Input
              placeholder="Do you offer emergency service?"
              value={form.question}
              onChange={(event) => setForm((current) => ({ ...current, question: event.target.value }))}
            />
          </div>
          <div>
            <label className="text-sm font-medium">Answer</label>
            <Input
              placeholder="Yes, 24/7."
              value={form.answer}
              onChange={(event) => setForm((current) => ({ ...current, answer: event.target.value }))}
            />
          </div>
          <div>
            <label className="text-sm font-medium">Category</label>
            <Input
              placeholder="General"
              value={form.category ?? ""}
              onChange={(event) => setForm((current) => ({ ...current, category: event.target.value }))}
            />
          </div>
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
