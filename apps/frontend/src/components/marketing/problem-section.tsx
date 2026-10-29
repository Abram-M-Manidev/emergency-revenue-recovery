import { Clock3, MoonStar, PhoneMissed, Repeat2 } from "lucide-react";

import { Container, SectionHeading } from "./primitives";
import { Reveal } from "./reveal";

const PROBLEMS = [
  {
    icon: PhoneMissed,
    title: "Busy periods bury the phone",
    body: "When every technician is on a job and the office is slammed, calls ring out. Callers rarely leave a voicemail — they call the next company.",
  },
  {
    icon: MoonStar,
    title: "After-hours calls go unanswered",
    body: "Burst pipes and failed furnaces don't wait for business hours. An answering service that only takes messages still leaves the job undecided.",
  },
  {
    icon: Clock3,
    title: "Emergencies miss the right workflow",
    body: "A true emergency mixed in with routine requests can sit in a voicemail box until morning instead of reaching the people who can act on it.",
  },
  {
    icon: Repeat2,
    title: "Follow-up is inconsistent",
    body: "Scribbled notes and missed callbacks mean interested customers slip away before anyone gets back to them.",
  },
] as const;

export function ProblemSection() {
  return (
    <section
      aria-labelledby="problem-title"
      className="border-t border-white/[0.06] py-24 sm:py-28"
    >
      <Container>
        <div className="grid gap-14 lg:grid-cols-[0.9fr_1.1fr] lg:gap-16">
          <SectionHeading
            id="problem-title"
            eyebrow="The problem"
            title="The calls you can't answer are the jobs you don't get."
            description="Service businesses don't lose work because they're bad at the job. They lose it in the gaps — between calls, after hours, and in the handoff from phone to schedule."
          />
          <div className="grid gap-px overflow-hidden rounded-2xl border border-white/[0.08] bg-white/[0.06] sm:grid-cols-2">
            {PROBLEMS.map(({ icon: Icon, title, body }, index) => (
              <Reveal key={title} delay={index * 0.05} className="bg-[#070b16] p-6 sm:p-7">
                <Icon className="h-5 w-5 text-cyan-300" aria-hidden="true" />
                <h3 className="mt-4 text-base font-semibold text-white">{title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-slate-400">{body}</p>
              </Reveal>
            ))}
          </div>
        </div>
      </Container>
    </section>
  );
}
