import type { ReactNode } from "react";
import { Siren } from "lucide-react";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-8 bg-muted/30 p-4">
      <div className="flex items-center gap-2">
        <Siren className="h-6 w-6 text-primary" aria-hidden="true" />
        <span className="text-base font-semibold tracking-tight">
          Emergency Revenue Recovery System
        </span>
      </div>
      <div className="w-full max-w-md">{children}</div>
    </div>
  );
}
