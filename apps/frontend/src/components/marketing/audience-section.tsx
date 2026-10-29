import { Droplets, Flame, Zap } from "lucide-react";

import { Container, SectionHeading } from "./primitives";
import { Reveal } from "./reveal";

const TRADES = [
  {
    icon: Flame,
    trade: "HVAC",
    lead: "No-heat and no-cooling calls rarely arrive at a convenient time.",
    examples: [
      "Furnace or boiler failure",
      "AC outage in extreme heat",
      "Seasonal tune-up bookings",
    ],
  },
  {
    icon: Droplets,
    trade: "Plumbing",
    lead: "Water damage gets worse by the minute, and callers know it.",
    examples: ["Burst or leaking pipes", "Sewer backups", "Water heater replacements"],
  },
  {
    icon: Zap,
    trade: "Electrical",
    lead: "Safety-related calls need a different path than routine quotes.",
    examples: [
      "Power loss or sparking outlets",
      "Panel and breaker issues",
      "Upgrade and install estimates",
    ],
  },
] as const;

const CONFIGURABLE = [
  "Services you offer",
  "Business hours",
  "Service areas",
  "Emergency keywords",
] as const;

export function AudienceSection() {
  return (
    <section
      aria-labelledby="audience-title"
      className="border-t border-white/[0.06] py-24 sm:py-28"
    >
      <Container>
        <SectionHeading
          id="audience-title"
          eyebrow="Who it's for"
          title="Built first for the trades where a missed call costs the most."
          description="ESSR is focused on home service businesses whose phones ring with urgent problems — and whose callers will go elsewhere if nobody answers."
        />

        <div className="mt-14 grid gap-5 md:grid-cols-3">
          {TRADES.map(({ icon: Icon, trade, lead, examples }, index) => (
            <Reveal key={trade} delay={index * 0.06} className="h-full">
              <article className="h-full rounded-2xl border border-white/[0.08] bg-[#080d19] p-7">
                <div className="flex items-center gap-3">
                  <Icon className="h-5 w-5 text-cyan-300" aria-hidden="true" />
                  <h3 className="text-lg font-semibold text-white">{trade}</h3>
                </div>
                <p className="mt-4 text-sm leading-relaxed text-slate-400">{lead}</p>
                <ul className="mt-5 space-y-2 border-t border-white/[0.06] pt-5">
                  {examples.map((example) => (
                    <li key={example} className="flex items-center gap-2.5 text-sm text-slate-300">
                      <span className="h-1 w-1 rounded-full bg-cyan-300" aria-hidden="true" />
                      {example}
                    </li>
                  ))}
                </ul>
              </article>
            </Reveal>
          ))}
        </div>

        <div className="mt-8 flex flex-col gap-5 rounded-2xl border border-cyan-300/15 bg-cyan-300/[0.04] p-7 md:flex-row md:items-center md:justify-between">
          <p className="max-w-md text-sm leading-relaxed text-slate-300">
            <span className="font-semibold text-white">
              Configured around your business, not a script.
            </span>{" "}
            You decide what counts as an emergency and what should be booked as routine work.
          </p>
          <ul className="flex flex-wrap gap-2" aria-label="What you can configure">
            {CONFIGURABLE.map((item) => (
              <li
                key={item}
                className="rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 text-xs text-slate-200"
              >
                {item}
              </li>
            ))}
          </ul>
        </div>
      </Container>
    </section>
  );
}
