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
  createEmergencyKeyword,
  deleteEmergencyKeyword,
  fetchEmergencyKeywords,
} from "@/lib/api/business-knowledge";
import { ApiError } from "@/lib/api/client";
import type { EmergencyKeyword } from "@/lib/api/types";
import { useToast } from "@/hooks/use-toast";

const EMPTY_FORM = { phrase: "", notes: "" };

export function EmergencyKeywordsSection() {
  const { toast } = useToast();
  const [isLoading, setIsLoading] = useState(true);
  const [keywords, setKeywords] = useState<EmergencyKeyword[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchEmergencyKeywords()
      .then((data) => {
        if (!cancelled) setKeywords(data);
      })
      .catch(() => toast({ title: "Failed to load emergency keywords", variant: "destructive" }))
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function addKeyword() {
    setIsSaving(true);
    try {
      const created = await createEmergencyKeyword({
        phrase: form.phrase,
        notes: form.notes || null,
      });
      setKeywords((current) => [...current, created]);
      setModalOpen(false);
      setForm(EMPTY_FORM);
      toast({ title: "Emergency keyword added", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to add keyword",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  }

  async function removeKeyword(id: string) {
    try {
      await deleteEmergencyKeyword(id);
      setKeywords((current) => current.filter((keyword) => keyword.id !== id));
    } catch {
      toast({ title: "Failed to remove keyword", variant: "destructive" });
    }
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="text-base">Emergency keywords</CardTitle>
          <CardDescription>Phrases that flag a call as an emergency.</CardDescription>
        </div>
        <Button variant="outline" size="sm" onClick={() => setModalOpen(true)}>
          <Plus className="h-4 w-4" /> Add keyword
        </Button>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : keywords.length === 0 ? (
          <EmptyState
            title="No emergency keywords yet"
            description="Add phrases like 'no heat' or 'gas leak' to flag emergencies."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Phrase</TableHead>
                <TableHead>Notes</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {keywords.map((keyword) => (
                <TableRow key={keyword.id}>
                  <TableCell>{keyword.phrase}</TableCell>
                  <TableCell>{keyword.notes ?? "—"}</TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label="Remove keyword"
                      onClick={() => removeKeyword(keyword.id)}
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
        title="Add emergency keyword"
        footer={
          <>
            <Button variant="outline" onClick={() => setModalOpen(false)}>
              Cancel
            </Button>
            <Button onClick={addKeyword} isLoading={isSaving}>
              Add
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="text-sm font-medium">Phrase</label>
            <Input
              placeholder="no heat"
              value={form.phrase}
              onChange={(event) => setForm((current) => ({ ...current, phrase: event.target.value }))}
            />
          </div>
          <div>
            <label className="text-sm font-medium">Notes</label>
            <Input
              placeholder="Winter emergency"
              value={form.notes}
              onChange={(event) => setForm((current) => ({ ...current, notes: event.target.value }))}
            />
          </div>
        </div>
      </Modal>
    </Card>
  );
}
