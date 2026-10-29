"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Menu, X } from "lucide-react";

import { ROUTES } from "@/lib/constants";
import { cn } from "@/lib/utils/cn";

import { NAV_LINKS } from "./nav-links";
import { Wordmark } from "./wordmark";
import { ctaPrimary, focusRing } from "./primitives";

export function SiteHeader() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <header className="sticky top-0 z-40 border-b border-white/[0.06] bg-[#060912]/80 backdrop-blur-md">
      <div className="mx-auto flex h-16 w-full max-w-6xl items-center justify-between px-5 sm:px-8">
        <Link href="/" aria-label="ESSR home" className={cn("rounded-md", focusRing)}>
          <Wordmark />
        </Link>

        <nav aria-label="Primary" className="hidden items-center gap-1 md:flex">
          {NAV_LINKS.map((link) => (
            <a
              key={link.href}
              href={link.href}
              className={cn(
                "rounded-md px-3 py-2 text-sm text-slate-300 transition-colors hover:text-white",
                focusRing,
              )}
            >
              {link.label}
            </a>
          ))}
        </nav>

        <div className="hidden items-center gap-3 md:flex">
          <Link
            href={ROUTES.login}
            className={cn(
              "rounded-md px-3 py-2 text-sm text-slate-300 hover:text-white",
              focusRing,
            )}
          >
            Sign in
          </Link>
          <a href="#early-access" className={cn(ctaPrimary, "px-4 py-2")}>
            Get Early Access
          </a>
        </div>

        <button
          type="button"
          className={cn("rounded-md p-2 text-slate-200 md:hidden", focusRing)}
          aria-expanded={open}
          aria-controls="mobile-menu"
          aria-label={open ? "Close menu" : "Open menu"}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? (
            <X className="h-5 w-5" aria-hidden="true" />
          ) : (
            <Menu className="h-5 w-5" aria-hidden="true" />
          )}
        </button>
      </div>

      {open ? (
        <nav
          id="mobile-menu"
          aria-label="Mobile"
          className="border-t border-white/[0.06] bg-[#060912] px-5 pb-6 pt-2 md:hidden"
        >
          <ul className="flex flex-col">
            {NAV_LINKS.map((link) => (
              <li key={link.href}>
                <a
                  href={link.href}
                  onClick={() => setOpen(false)}
                  className={cn(
                    "block rounded-md px-2 py-3 text-base text-slate-200 hover:text-white",
                    focusRing,
                  )}
                >
                  {link.label}
                </a>
              </li>
            ))}
            <li>
              <Link
                href={ROUTES.login}
                onClick={() => setOpen(false)}
                className={cn("block rounded-md px-2 py-3 text-base text-slate-400", focusRing)}
              >
                Sign in
              </Link>
            </li>
          </ul>
          <a
            href="#early-access"
            onClick={() => setOpen(false)}
            className={cn(ctaPrimary, "mt-4 w-full")}
          >
            Get Early Access
          </a>
        </nav>
      ) : null}
    </header>
  );
}
