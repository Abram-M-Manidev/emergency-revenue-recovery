import type { ReactNode } from "react";

import { cn } from "@/lib/utils/cn";

/**
 * Shared building blocks for the public marketing site. The site uses its
 * own fixed dark palette rather than the dashboard's theme tokens so the
 * dashboard's light theme is untouched.
 */

export const focusRing =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300 focus-visible:ring-offset-2 focus-visible:ring-offset-[#060912]";

export const ctaPrimary = cn(
  "inline-flex items-center justify-center gap-2 rounded-lg bg-cyan-400 px-5 py-3 text-sm font-semibold text-[#04121a] shadow-[0_0_0_1px_rgba(34,211,238,0.4)] transition-colors hover:bg-cyan-300",
  focusRing,
);

export const ctaSecondary = cn(
  "inline-flex items-center justify-center gap-2 rounded-lg border border-white/15 bg-white/[0.03] px-5 py-3 text-sm font-semibold text-white transition-colors hover:border-white/30 hover:bg-white/[0.06]",
  focusRing,
);

export function Container({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn("mx-auto w-full max-w-6xl px-5 sm:px-8", className)}>{children}</div>;
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <p className="text-xs font-semibold uppercase tracking-[0.18em] text-cyan-300">{children}</p>
  );
}

export function SectionHeading({
  eyebrow,
  title,
  description,
  id,
  align = "left",
}: {
  eyebrow: string;
  title: ReactNode;
  description?: ReactNode;
  id?: string;
  align?: "left" | "center";
}) {
  return (
    <div className={cn("max-w-2xl space-y-4", align === "center" && "mx-auto text-center")}>
      <Eyebrow>{eyebrow}</Eyebrow>
      <h2 id={id} className="text-3xl font-semibold tracking-tight text-white sm:text-4xl">
        {title}
      </h2>
      {description ? (
        <p className="text-base leading-relaxed text-slate-400">{description}</p>
      ) : null}
    </div>
  );
}

/** Small label that marks a visual as fictional so it can't be mistaken for a live account. */
export function SampleDataBadge({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border border-amber-300/30 bg-amber-300/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-200",
        className,
      )}
    >
      Illustrative sample data
    </span>
  );
}
