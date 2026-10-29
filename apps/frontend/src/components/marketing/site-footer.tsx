import Link from "next/link";

import { ROUTES } from "@/lib/constants";
import { cn } from "@/lib/utils/cn";

import { NAV_LINKS } from "./nav-links";
import { Container, focusRing } from "./primitives";
import { Wordmark } from "./wordmark";

const linkClass = cn(
  "rounded text-sm text-slate-400 transition-colors hover:text-white",
  focusRing,
);

export function SiteFooter() {
  const year = new Date().getFullYear();

  return (
    <footer className="border-t border-white/[0.06] bg-[#05070e]">
      <Container className="grid gap-10 py-14 md:grid-cols-[1.4fr_1fr_1fr]">
        <div className="space-y-4">
          <Wordmark />
          <p className="max-w-xs text-sm leading-relaxed text-slate-500">
            Emergency Revenue Recovery System — AI call handling for HVAC, plumbing and electrical
            service businesses.
          </p>
        </div>

        <nav aria-label="Product">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-300">
            Product
          </p>
          <ul className="mt-4 space-y-3">
            {NAV_LINKS.map((link) => (
              <li key={link.href}>
                <a href={link.href} className={linkClass}>
                  {link.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <nav aria-label="Get started">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-300">
            Get started
          </p>
          <ul className="mt-4 space-y-3">
            <li>
              <a href="#early-access" className={linkClass}>
                Request early access
              </a>
            </li>
            <li>
              <Link href={ROUTES.login} className={linkClass}>
                Customer sign in
              </Link>
            </li>
          </ul>
        </nav>
      </Container>

      <Container className="border-t border-white/[0.06] py-6">
        <p className="text-xs text-slate-600">© {year} ESSR. All rights reserved.</p>
      </Container>
    </footer>
  );
}
