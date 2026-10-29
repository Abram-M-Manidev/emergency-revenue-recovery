import { ArrowRight } from "lucide-react";

import { CallFlowVisual } from "./call-flow-visual";
import { Container, ctaPrimary, ctaSecondary } from "./primitives";

export function Hero() {
  return (
    <section aria-labelledby="hero-title" className="relative overflow-hidden">
      {/* Restrained backdrop: one soft gradient and a faint grid. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_60%_50%_at_70%_0%,rgba(34,211,238,0.12),transparent_70%)]"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[linear-gradient(rgba(255,255,255,0.025)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,0.025)_1px,transparent_1px)] bg-[size:56px_56px] [mask-image:radial-gradient(ellipse_80%_60%_at_50%_0%,black,transparent)]"
      />

      <Container className="relative grid items-center gap-14 pb-20 pt-16 sm:pt-20 lg:grid-cols-[1.05fr_1fr] lg:gap-12 lg:pb-28 lg:pt-24">
        <div className="space-y-8">
          <p className="inline-flex items-center gap-2 rounded-full border border-cyan-300/20 bg-cyan-300/[0.06] px-3 py-1 text-xs font-medium text-cyan-200">
            <span className="h-1.5 w-1.5 rounded-full bg-cyan-300" aria-hidden="true" />
            Built for HVAC, plumbing &amp; electrical service businesses
          </p>

          <div className="space-y-6">
            <h1
              id="hero-title"
              className="text-4xl font-semibold leading-[1.05] tracking-tight text-white sm:text-5xl lg:text-[3.6rem]"
            >
              Every missed call is a job <span className="text-cyan-300">you could lose.</span>
            </h1>
            <p className="max-w-xl text-lg leading-relaxed text-slate-400">
              ESSR answers incoming calls with an AI voice agent, understands what the caller needs,
              and helps turn after-hours and overflow calls into booked appointments or actionable
              emergency tickets — following the rules you set for your business.
            </p>
          </div>

          <div className="flex flex-col gap-3 sm:flex-row">
            <a href="#early-access" className={ctaPrimary}>
              Get Early Access
              <ArrowRight className="h-4 w-4" aria-hidden="true" />
            </a>
            <a href="#how-it-works" className={ctaSecondary}>
              See How It Works
            </a>
          </div>

          <p className="text-sm text-slate-500">
            Now onboarding a small group of early-access service businesses.
          </p>
        </div>

        <CallFlowVisual />
      </Container>
    </section>
  );
}
