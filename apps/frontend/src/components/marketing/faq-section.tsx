import { Plus } from "lucide-react";

import { Container, SectionHeading, focusRing } from "./primitives";

const FAQS = [
  {
    q: "What is ESSR?",
    a: "ESSR (Emergency Revenue Recovery System) is an AI-powered call-handling system for home service businesses. It answers incoming calls, works out what the caller needs, and turns the call into a concrete outcome — a booked appointment, an emergency dispatch ticket, or a follow-up for your team — while keeping a record of every interaction.",
  },
  {
    q: "Who is ESSR designed for?",
    a: "We're starting with HVAC, plumbing and electrical service companies — businesses that receive urgent calls, often outside office hours, where a missed call usually means a lost job.",
  },
  {
    q: "Can ESSR handle calls after business hours?",
    a: "Yes. Handling calls when your office is closed or your team is busy is the core use case. You configure your business hours, and ESSR uses them when deciding how to respond.",
  },
  {
    q: "Can it book appointments?",
    a: "Yes. ESSR checks available times, offers them to the caller, and books only a time the caller agrees to. Appointments appear in your dashboard. ESSR uses its own scheduling today; connections to third-party field-service or calendar software are not available yet.",
  },
  {
    q: "How are emergency requests handled?",
    a: "Emergency keywords and rules you configure help ESSR recognize urgent situations. When a call qualifies, ESSR collects the details and creates an emergency dispatch ticket for your team. Notifying on-call staff is part of what we're validating with early-access customers. ESSR is not a substitute for 911: callers who report hazards such as a gas leak or a carbon monoxide alarm are told to get to safety and contact emergency services, and ESSR cannot contact emergency services itself.",
  },
  {
    q: "Does ESSR replace my existing team?",
    a: "No. ESSR covers the calls your team can't get to — after hours, during busy periods, or while everyone is on a job — and hands them back as organized records. Your dispatchers and technicians still make the decisions and do the work.",
  },
  {
    q: "How can I request early access?",
    a: "Early-access requests will be taken through the form on this page; it's shown as a preview and doesn't submit yet. We're onboarding a small number of service businesses and will work with each one to configure services, hours and emergency rules before any calls are handled.",
  },
] as const;

export function FaqSection() {
  return (
    <section
      id="faq"
      aria-labelledby="faq-title"
      className="scroll-mt-20 border-t border-white/[0.06] bg-[#070b16] py-24 sm:py-28"
    >
      <Container className="grid gap-12 lg:grid-cols-[0.8fr_1.2fr] lg:gap-16">
        <SectionHeading
          id="faq-title"
          eyebrow="FAQ"
          title="Questions owners ask first."
          description="Straight answers about what ESSR does today and what's still in progress."
        />

        {/* Native <details> keeps the accordion keyboard-accessible without client JS. */}
        <div className="divide-y divide-white/[0.08] border-y border-white/[0.08]">
          {FAQS.map(({ q, a }) => (
            <details key={q} className="group">
              <summary
                className={`flex cursor-pointer list-none items-center justify-between gap-6 rounded-md py-5 text-left text-base font-medium text-white [&::-webkit-details-marker]:hidden ${focusRing}`}
              >
                {q}
                <Plus
                  className="h-4 w-4 shrink-0 text-cyan-300 transition-transform duration-200 group-open:rotate-45 motion-reduce:transition-none"
                  aria-hidden="true"
                />
              </summary>
              <p className="pb-6 pr-10 text-sm leading-relaxed text-slate-400">{a}</p>
            </details>
          ))}
        </div>
      </Container>
    </section>
  );
}
