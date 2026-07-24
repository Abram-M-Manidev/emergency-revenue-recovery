"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { BookOpen, LayoutDashboard, Settings, Siren } from "lucide-react";

import { ROUTES } from "@/lib/constants";
import { cn } from "@/lib/utils/cn";

const NAV_ITEMS = [
  { href: ROUTES.dashboard, label: "Dashboard", icon: LayoutDashboard },
  { href: ROUTES.businessKnowledge, label: "Business Knowledge", icon: BookOpen },
  { href: ROUTES.settings, label: "Settings", icon: Settings },
] as const;

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="hidden w-64 shrink-0 flex-col border-r border-border bg-card md:flex">
      <div className="flex h-16 items-center gap-2 border-b border-border px-6">
        <Siren className="h-5 w-5 text-primary" aria-hidden="true" />
        <span className="text-sm font-semibold tracking-tight">ERRS</span>
      </div>

      <nav className="flex-1 space-y-1 p-3">
        {NAV_ITEMS.map((item) => {
          const isActive = pathname?.startsWith(item.href);
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                isActive
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              <Icon className="h-4 w-4" aria-hidden="true" />
              {item.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
