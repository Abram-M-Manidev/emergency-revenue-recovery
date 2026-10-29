import {
  AudioLines,
  BarChart3,
  BookOpenCheck,
  CalendarClock,
  Siren,
  UsersRound,
} from "lucide-react";

import { Container, SectionHeading } from "./primitives";
import { Reveal } from "./reveal";

// Copy is limited to what the current product implements. Anything still
// being validated in the pilot is worded as such.
const FEATURES = [
  {
    icon: AudioLines,
    title: "AI voice call handling",
    body: "A conversational voice agent answers calls, asks follow-up questions and keeps the conversation on the caller's problem. A configurable disclosure tells callers they're speaking with an AI assistant; live transfer to your staff is being validated in early access.",
  },
  {
    icon: BookOpenCheck,
    title: "Business-specific knowledge and rules",
    body: "Configure your services, business hours, service areas, FAQs and emergency keywords. The agent answers from what you've entered rather than guessing.",
  },
  {
    icon: CalendarClock,
    title: "Appointment availability and booking",
    body: "ESSR checks open times, offers them to the caller and books only a slot the caller actually chose — no appointment is created without their agreement.",
  },
  {
    icon: Siren,
    title: "Emergency intake and dispatch tickets",
    body: "Calls that match your emergency rules become dispatch tickets with the details your technicians need. On-call staff notifications are being validated with early-access customers.",
  },
  {
    icon: UsersRound,
    title: "Customer records and history",
    body: "Callers are matched to customer records, so contact details, past conversations, appointments and tickets live in one place.",
  },
  {
    icon: BarChart3,
    title: "Operational visibility and analytics",
    body: "See call volume, how calls were classified, appointment outcomes and ticket resolution over time — plus a queue of calls that still need follow-up.",
  },
] as const;

export function FeaturesSection() {
  return (
    <section
      id="features"
      aria-labelledby="features-title"
      className="scroll-mt-20 border-t border-white/[0.06] py-24 sm:py-28"
    >
      <Container>
        <SectionHeading
          id="features-title"
          eyebrow="Features"
          title="More than an answering service."
          description="ESSR doesn't just take a message. It follows your rules to move each call to the right outcome, and keeps a record your team can act on."
        />

        <div className="mt-14 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map(({ icon: Icon, title, body }, index) => (
            <Reveal key={title} delay={(index % 3) * 0.06} className="h-full">
              <article className="group h-full rounded-2xl border border-white/[0.08] bg-gradient-to-b from-white/[0.035] to-transparent p-7 transition-colors hover:border-cyan-300/25">
                <span className="flex h-10 w-10 items-center justify-center rounded-lg border border-white/10 bg-[#0b1324] text-cyan-300">
                  <Icon className="h-5 w-5" aria-hidden="true" />
                </span>
                <h3 className="mt-6 text-lg font-semibold text-white">{title}</h3>
                <p className="mt-3 text-sm leading-relaxed text-slate-400">{body}</p>
              </article>
            </Reveal>
          ))}
        </div>
      </Container>
    </section>
  );
}
