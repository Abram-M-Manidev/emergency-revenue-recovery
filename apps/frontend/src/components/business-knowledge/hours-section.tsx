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
  createHoursException,
  deleteHoursException,
  fetchHoursExceptions,
  fetchWeeklyHours,
  replaceWeeklyHours,
} from "@/lib/api/business-knowledge";
import { ApiError } from "@/lib/api/client";
import type { HoursException, WeeklyHoursEntry } from "@/lib/api/types";
import { useToast } from "@/hooks/use-toast";

const DAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function defaultWeek(): WeeklyHoursEntry[] {
  return DAY_LABELS.map((_, day) => ({
    day_of_week: day,
    is_closed: day >= 5,
    open_time: day >= 5 ? null : "08:00",
    close_time: day >= 5 ? null : "17:00",
  }));
}

const EMPTY_EXCEPTION = { date: "", is_closed: true, open_time: "", close_time: "", label: "" };

export function HoursSection() {
  const { toast } = useToast();
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [entries, setEntries] = useState<WeeklyHoursEntry[]>(defaultWeek());
  const [exceptions, setExceptions] = useState<HoursException[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [exceptionForm, setExceptionForm] = useState(EMPTY_EXCEPTION);
  const [isCreatingException, setIsCreatingException] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const [weekly, exceptionList] = await Promise.all([
          fetchWeeklyHours(),
          fetchHoursExceptions(),
        ]);
        if (cancelled) return;
        if (weekly.length > 0) {
          setEntries(
            [...weekly]
              .sort((a, b) => a.day_of_week - b.day_of_week)
              .map(({ day_of_week, is_closed, open_time, close_time }) => ({
                day_of_week,
                is_closed,
                open_time,
                close_time,
              })),
          );
        }
        setExceptions(exceptionList);
      } catch {
        toast({ title: "Failed to load business hours", variant: "destructive" });
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function updateDay(day: number, patch: Partial<WeeklyHoursEntry>) {
    setEntries((current) =>
      current.map((entry) => (entry.day_of_week === day ? { ...entry, ...patch } : entry)),
    );
  }

  async function saveWeek() {
    setIsSaving(true);
    try {
      const saved = await replaceWeeklyHours(entries);
      setEntries(
        [...saved]
          .sort((a, b) => a.day_of_week - b.day_of_week)
          .map(({ day_of_week, is_closed, open_time, close_time }) => ({
            day_of_week,
            is_closed,
            open_time,
            close_time,
          })),
      );
      toast({ title: "Business hours saved", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to save business hours",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  }

  async function addException() {
    setIsCreatingException(true);
    try {
      const created = await createHoursException({
        date: exceptionForm.date,
        is_closed: exceptionForm.is_closed,
        open_time: exceptionForm.is_closed ? null : exceptionForm.open_time || null,
        close_time: exceptionForm.is_closed ? null : exceptionForm.close_time || null,
        label: exceptionForm.label || null,
      });
      setExceptions((current) => [...current, created].sort((a, b) => a.date.localeCompare(b.date)));
      setModalOpen(false);
      setExceptionForm(EMPTY_EXCEPTION);
      toast({ title: "Exception added", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to add exception",
        variant: "destructive",
      });
    } finally {
      setIsCreatingException(false);
    }
  }

  async function removeException(id: string) {
    try {
      await deleteHoursException(id);
      setExceptions((current) => current.filter((item) => item.id !== id));
    } catch {
      toast({ title: "Failed to remove exception", variant: "destructive" });
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Hours of operation</CardTitle>
        <CardDescription>
          Used to determine when a call is after-hours. Add holidays or one-off closures below.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : (
          <>
            <div className="space-y-2">
              {entries.map((entry) => (
                <div
                  key={entry.day_of_week}
                  className="flex flex-wrap items-center gap-3 rounded-md border border-border p-3 text-sm"
                >
                  <span className="w-24 shrink-0 font-medium">{DAY_LABELS[entry.day_of_week]}</span>
                  <label className="flex items-center gap-2 text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={entry.is_closed}
                      onChange={(event) =>
                        updateDay(entry.day_of_week, { is_closed: event.target.checked })
                      }
                    />
                    Closed
                  </label>
                  {!entry.is_closed && (
                    <>
                      <input
                        type="time"
                        value={entry.open_time ?? ""}
                        onChange={(event) =>
                          updateDay(entry.day_of_week, { open_time: event.target.value })
                        }
                        className="h-9 rounded-md border border-input bg-background px-2 text-sm"
                      />
                      <span className="text-muted-foreground">to</span>
                      <input
                        type="time"
                        value={entry.close_time ?? ""}
                        onChange={(event) =>
                          updateDay(entry.day_of_week, { close_time: event.target.value })
                        }
                        className="h-9 rounded-md border border-input bg-background px-2 text-sm"
                      />
                    </>
                  )}
                </div>
              ))}
              <Button onClick={saveWeek} isLoading={isSaving}>
                Save hours
              </Button>
            </div>

            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <h4 className="text-sm font-medium">Holidays &amp; exceptions</h4>
                <Button variant="outline" size="sm" onClick={() => setModalOpen(true)}>
                  <Plus className="h-4 w-4" /> Add exception
                </Button>
              </div>
              {exceptions.length === 0 ? (
                <EmptyState title="No exceptions configured" description="Holidays and one-off closures will appear here." />
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Date</TableHead>
                      <TableHead>Label</TableHead>
                      <TableHead>Hours</TableHead>
                      <TableHead />
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {exceptions.map((exception) => (
                      <TableRow key={exception.id}>
                        <TableCell>{exception.date}</TableCell>
                        <TableCell>{exception.label ?? "—"}</TableCell>
                        <TableCell>
                          {exception.is_closed
                            ? "Closed"
                            : `${exception.open_time} – ${exception.close_time}`}
                        </TableCell>
                        <TableCell>
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label="Remove exception"
                            onClick={() => removeException(exception.id)}
                          >
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </div>
          </>
        )}
      </CardContent>

      <Modal
        open={modalOpen}
        onOpenChange={setModalOpen}
        title="Add hours exception"
        description="Holidays or one-off days with different hours."
        footer={
          <>
            <Button variant="outline" onClick={() => setModalOpen(false)}>
              Cancel
            </Button>
            <Button onClick={addException} isLoading={isCreatingException}>
              Add
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="text-sm font-medium">Date</label>
            <Input
              type="date"
              value={exceptionForm.date}
              onChange={(event) =>
                setExceptionForm((current) => ({ ...current, date: event.target.value }))
              }
            />
          </div>
          <div>
            <label className="text-sm font-medium">Label</label>
            <Input
              placeholder="Christmas Day"
              value={exceptionForm.label}
              onChange={(event) =>
                setExceptionForm((current) => ({ ...current, label: event.target.value }))
              }
            />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={exceptionForm.is_closed}
              onChange={(event) =>
                setExceptionForm((current) => ({ ...current, is_closed: event.target.checked }))
              }
            />
            Closed all day
          </label>
          {!exceptionForm.is_closed && (
            <div className="flex gap-3">
              <div>
                <label className="text-sm font-medium">Open</label>
                <input
                  type="time"
                  value={exceptionForm.open_time}
                  onChange={(event) =>
                    setExceptionForm((current) => ({ ...current, open_time: event.target.value }))
                  }
                  className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="text-sm font-medium">Close</label>
                <input
                  type="time"
                  value={exceptionForm.close_time}
                  onChange={(event) =>
                    setExceptionForm((current) => ({ ...current, close_time: event.target.value }))
                  }
                  className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                />
              </div>
            </div>
          )}
        </div>
      </Modal>
    </Card>
  );
}
