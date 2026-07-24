import type { ReactNode } from "react";

import { Sidebar } from "@/components/layout/sidebar";
import { TopNav } from "@/components/layout/topnav";

export function DashboardShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex flex-1 flex-col">
        <TopNav />
        <main className="flex-1 bg-muted/30 p-4 md:p-8">{children}</main>
      </div>
    </div>
  );
}
