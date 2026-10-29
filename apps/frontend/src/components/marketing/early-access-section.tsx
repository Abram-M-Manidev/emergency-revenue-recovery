import { Check } from "lucide-react";

import { EarlyAccessForm } from "./early-access-form";
import { Container, Eyebrow } from "./primitives";

const EXPECTATIONS = [
  "A walkthrough of how ESSR would handle your calls",
  "Help configuring your services, hours and emergency rules",
  "Direct input into what we build next",
] as const;

export function EarlyAccessSection() {
  return (
    <section
      id="early-access"
      aria-labelledby="early-access-title"
      className="relative scroll-mt-20 overflow-hidden border-t border-white/[0.06] py-24 sm:py-32"
    >
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_50%_60%_at_20%_50%,rgba(34,211,238,0.08),transparent_70%)]"
      />
      <Container className="relative grid items-center gap-12 lg:grid-cols-2 lg:gap-16">
        <div className="space-y-6">
          <Eyebrow>Early access</Eyebrow>
          <h2
            id="early-access-title"
            className="text-3xl font-semibold tracking-tight text-white sm:text-5xl sm:leading-[1.08]"
          >
            Stop letting the next call go to voicemail.
          </h2>
          <p className="max-w-lg text-lg leading-relaxed text-slate-400">
            We&apos;re working with a small group of HVAC, plumbing and electrical businesses to put
            ESSR on real phone lines. Early-access requests will open here soon, and we&apos;ll
            follow up with each business about fit and timing.
          </p>
          <ul className="space-y-3">
            {EXPECTATIONS.map((item) => (
              <li key={item} className="flex items-start gap-3 text-sm text-slate-300">
                <Check className="mt-0.5 h-4 w-4 shrink-0 text-cyan-300" aria-hidden="true" />
                {item}
              </li>
            ))}
          </ul>
        </div>

        <EarlyAccessForm />
      </Container>
    </section>
  );
}
