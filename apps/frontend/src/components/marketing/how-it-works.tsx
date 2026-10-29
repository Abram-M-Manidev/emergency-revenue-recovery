import { ClipboardList, GitBranch, MessagesSquare, Phone, Zap } from "lucide-react";

import { Container, SectionHeading } from "./primitives";
import { Reveal } from "./reveal";

const STEPS = [
  {
    icon: Phone,
    name: "Answer",
    body: "ESSR's AI voice agent picks up the incoming call — including after hours and when your team is busy.",
  },
  {
    icon: MessagesSquare,
    name: "Understand",
    body: "It asks about the problem and collects the caller's name, callback number, address and service details.",
  },
  {
    icon: GitBranch,
    name: "Classify",
    body: "Your business rules — services, hours, service areas and emergency keywords — help separate routine requests from emergencies.",
  },
  {
    icon: Zap,
    name: "Act",
    body: "For routine work it checks availability and books a time the caller agrees to. For emergencies it creates a dispatch ticket.",
  },
  {
    icon: ClipboardList,
    name: "Track",
    body: "Customer details, conversation history, appointments and tickets stay organized in one dashboard for your team.",
  },
] as const;

export function HowItWorks() {
  return (
    <section
      id="how-it-works"
      aria-labelledby="how-title"
      className="scroll-mt-20 border-t border-white/[0.06] bg-[#070b16] py-24 sm:py-28"
    >
      <Container>
        <SectionHeading
          id="how-title"
          eyebrow="How it works"
          title="From ringing phone to a clear next step."
          description="Every call follows the same five-stage workflow, shaped by the rules you configure for your business."
        />

        <ol className="relative mt-16 grid gap-10 lg:grid-cols-5 lg:gap-6">
          {/* Connector line: vertical on small screens, horizontal on large. */}
          <span
            aria-hidden="true"
            className="absolute bottom-4 left-[19px] top-4 w-px bg-gradient-to-b from-cyan-300/50 via-cyan-300/20 to-transparent lg:bottom-auto lg:left-4 lg:right-4 lg:top-[19px] lg:h-px lg:w-auto lg:bg-gradient-to-r"
          />
          {STEPS.map(({ icon: Icon, name, body }, index) => (
            <li key={name} className="relative">
              <Reveal delay={index * 0.07} className="flex gap-5 lg:flex-col lg:gap-0">
                <span className="relative z-10 flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-cyan-300/30 bg-[#0b1324] text-cyan-300">
                  <Icon className="h-[18px] w-[18px]" aria-hidden="true" />
                </span>
                <div className="lg:mt-6">
                  <p className="font-mono text-xs text-slate-500">0{index + 1}</p>
                  <h3 className="mt-1 text-lg font-semibold text-white">{name}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-slate-400">{body}</p>
                </div>
              </Reveal>
            </li>
          ))}
        </ol>
      </Container>
    </section>
  );
}
