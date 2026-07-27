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
import { useAuth } from "@/hooks/use-auth";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api/client";
import {
  fetchTeamMembers,
  inviteTeamMember,
  setTeamMemberActive,
  setTeamMemberRole,
} from "@/lib/api/team";
import type { AssignableRole, InviteTeamMemberPayload, TeamMember } from "@/lib/api/types";

const EMPTY_FORM: InviteTeamMemberPayload = {
  full_name: "",
  email: "",
  temporary_password: "",
  role: "Member",
};

const ASSIGNABLE_ROLES: AssignableRole[] = ["Owner", "Admin", "Member"];

const SELECT_CLASSNAME =
  "flex h-9 rounded-md border border-input bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50";

export function TeamRoster() {
  const { user } = useAuth();
  const { toast } = useToast();
  const [isLoading, setIsLoading] = useState(true);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [form, setForm] = useState<InviteTeamMemberPayload>(EMPTY_FORM);
  const [isSaving, setIsSaving] = useState(false);

  const canManage = user?.permissions.includes("users:manage") ?? false;

  useEffect(() => {
    let cancelled = false;
    fetchTeamMembers()
      .then((data) => {
        if (!cancelled) setMembers(data);
      })
      .catch(() => toast({ title: "Failed to load team members", variant: "destructive" }))
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function openInvite() {
    setForm(EMPTY_FORM);
    setModalOpen(true);
  }

  async function save() {
    setIsSaving(true);
    try {
      const created = await inviteTeamMember(form);
      setMembers((current) => [...current, created]);
      setModalOpen(false);
      toast({ title: "Team member invited", variant: "success" });
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to invite team member",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  }

  async function toggleActive(member: TeamMember) {
    try {
      const updated = await setTeamMemberActive(member.id, !member.is_active);
      setMembers((current) =>
        current.map((m) => (m.id === member.id ? { ...m, is_active: updated.is_active } : m)),
      );
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to update member status",
        variant: "destructive",
      });
    }
  }

  async function changeRole(member: TeamMember, role: AssignableRole) {
    try {
      const updated = await setTeamMemberRole(member.id, role);
      setMembers((current) =>
        current.map((m) => (m.id === member.id ? { ...m, roles: updated.roles } : m)),
      );
    } catch (error) {
      toast({
        title: error instanceof ApiError ? error.message : "Failed to update member role",
        variant: "destructive",
      });
    }
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="text-base">Team members</CardTitle>
          <CardDescription>Everyone with a login for this organization.</CardDescription>
        </div>
        {canManage ? (
          <Button variant="outline" size="sm" onClick={openInvite}>
            <Plus className="h-4 w-4" /> Invite member
          </Button>
        ) : null}
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : members.length === 0 ? (
          <EmptyState title="No team members" description="Invite a teammate to get started." />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Email</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Status</TableHead>
                {canManage ? <TableHead /> : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {members.map((member) => {
                const isSelf = member.id === user?.id;
                const primaryRole = member.roles[0] ?? "—";
                const isTechnician = member.roles.includes("Technician");
                return (
                  <TableRow key={member.id}>
                    <TableCell>{member.full_name}</TableCell>
                    <TableCell>{member.email}</TableCell>
                    <TableCell>
                      {canManage && !isTechnician ? (
                        <select
                          className={SELECT_CLASSNAME}
                          value={primaryRole}
                          onChange={(event) =>
                            changeRole(member, event.target.value as AssignableRole)
                          }
                        >
                          {ASSIGNABLE_ROLES.map((role) => (
                            <option key={role} value={role}>
                              {role}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <Badge variant="secondary">{primaryRole}</Badge>
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge variant={member.is_active ? "success" : "secondary"}>
                        {member.is_active ? "Active" : "Inactive"}
                      </Badge>
                    </TableCell>
                    {canManage ? (
                      <TableCell>
                        {!isSelf ? (
                          <Button variant="ghost" size="sm" onClick={() => toggleActive(member)}>
                            {member.is_active ? "Deactivate" : "Reactivate"}
                          </Button>
                        ) : null}
                      </TableCell>
                    ) : null}
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>

      <Modal
        open={modalOpen}
        onOpenChange={setModalOpen}
        title="Invite member"
        description="Creates a login for this teammate. Share the temporary password with them yourself — there is no invite email."
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
          <div>
            <label className="text-sm font-medium">Role</label>
            <select
              className={`${SELECT_CLASSNAME} w-full`}
              value={form.role}
              onChange={(event) =>
                setForm((current) => ({ ...current, role: event.target.value as "Admin" | "Member" }))
              }
            >
              <option value="Admin">Admin</option>
              <option value="Member">Member</option>
            </select>
          </div>
        </div>
      </Modal>
    </Card>
  );
}
