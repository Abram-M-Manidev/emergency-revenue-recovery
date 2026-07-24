"use client";

import { useState } from "react";
import { LogOut, Menu } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/hooks/use-auth";
import { MobileNav } from "@/components/layout/mobile-nav";

export function TopNav() {
  const { user, logout } = useAuth();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  return (
    <header className="flex h-16 items-center justify-between border-b border-border bg-background px-4 md:px-6">
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => setMobileNavOpen(true)}
          aria-label="Open navigation"
          className="text-muted-foreground hover:text-foreground md:hidden"
        >
          <Menu className="h-5 w-5" />
        </button>
        <span className="text-sm font-medium text-muted-foreground">Welcome back{user ? `, ${user.full_name}` : ""}</span>
      </div>

      <div className="flex items-center gap-3">
        {user ? (
          <span className="hidden text-sm text-muted-foreground sm:inline">{user.email}</span>
        ) : null}
        <Button variant="ghost" size="sm" onClick={() => void logout()}>
          <LogOut className="h-4 w-4" aria-hidden="true" />
          Log out
        </Button>
      </div>

      <MobileNav open={mobileNavOpen} onOpenChange={setMobileNavOpen} />
    </header>
  );
}
