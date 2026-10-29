import {
  BarChart3,
  CalendarDays,
  LayoutDashboard,
  MessageSquare,
  Siren,
  Users,
} from "lucide-react";

import { cn } from "@/lib/utils/cn";

import { Container, SampleDataBadge, SectionHeading } from "./primitives";
import { Reveal } from "./reveal";

// Every name, number and address below is invented for illustration.
const SUMMARY = [
  { label: "Calls handled", value: "14", note: "since 6 PM" },
  { label: "Appointments booked", value: "5", note: "confirmed by caller" },
  { label: "Emergency tickets", value: "2", note: "1 open" },
  { label: "Needs follow-up", value: "3", note: "callback requested" },
] as const;

type Tone = "emergency" | "booked" | "followup" | "info";

const CALLS: { time: string; caller: string; issue: string; outcome: string; tone: Tone }[] = [
  {
    time: "9:47 PM",
    caller: "Dana R.",
    issue: "No heat, elderly occupant",
    outcome: "Emergency ticket",
    tone: "emergency",
  },
  {
    time: "9:12 PM",
    caller: "Marcus T.",
    issue: "Water heater leaking slowly",
    outcome: "Booked Thu 10:00",
    tone: "booked",
  },
  {
    time: "8:30 PM",
    caller: "Priya S.",
    issue: "Quote for panel upgrade",
    outcome: "Follow-up",
    tone: "followup",
  },
  {
    time: "7:58 PM",
    caller: "Leo K.",
    issue: "AC tune-up",
    outcome: "Booked Fri 2:00",
    tone: "booked",
  },
  {
    time: "7:05 PM",
    caller: "Unknown",
    issue: "Asked about service area",
    outcome: "Answered",
    tone: "info",
  },
];

const TONE_CLASS: Record<Tone, string> = {
  emergency: "border-rose-400/25 bg-rose-400/10 text-rose-200",
  booked: "border-emerald-400/25 bg-emerald-400/10 text-emerald-200",
  followup: "border-amber-300/25 bg-amber-300/10 text-amber-100",
  info: "border-white/10 bg-white/[0.05] text-slate-300",
};

const NAV = [
  { icon: LayoutDashboard, label: "Overview", active: true },
  { icon: MessageSquare, label: "Conversations" },
  { icon: CalendarDays, label: "Appointments" },
  { icon: Siren, label: "Dispatch" },
  { icon: Users, label: "Customers" },
  { icon: BarChart3, label: "Analytics" },
] as const;

export function ProductPreview() {
  return (
    <section
      id="product"
      aria-labelledby="product-title"
      className="scroll-mt-20 border-t border-white/[0.06] bg-[#070b16] py-24 sm:py-28"
    >
      <Container>
        <div className="flex flex-col gap-6 md:flex-row md:items-end md:justify-between">
          <SectionHeading
            id="product-title"
            eyebrow="Product"
            title="One view of what happened while you were off the phone."
            description="The operator dashboard brings calls, bookings, emergency tickets and customer details together, so the morning starts with a clear list instead of a voicemail backlog."
          />
          <SampleDataBadge className="self-start md:self-end" />
        </div>

        <Reveal className="mt-12">
          <div
            role="img"
            aria-label="Illustrative ESSR dashboard showing an overnight summary, a list of recent calls with their outcomes, an open emergency ticket and an upcoming appointment. All data is fictional."
            className="overflow-hidden rounded-2xl border border-white/10 bg-[#0a101e] shadow-2xl shadow-black/50"
          >
            {/* Window chrome */}
            <div className="flex items-center gap-2 border-b border-white/[0.06] bg-[#0c1324] px-4 py-3">
              <span className="h-2.5 w-2.5 rounded-full bg-white/15" />
              <span className="h-2.5 w-2.5 rounded-full bg-white/15" />
              <span className="h-2.5 w-2.5 rounded-full bg-white/15" />
              <span className="ml-3 hidden rounded-md bg-white/[0.04] px-3 py-1 text-[11px] text-slate-500 sm:inline">
                Example Heating &amp; Plumbing · Overview
              </span>
            </div>

            <div className="flex">
              {/* Sidebar */}
              <div className="hidden w-48 shrink-0 border-r border-white/[0.06] p-3 lg:block">
                {NAV.map(({ icon: Icon, label, ...rest }) => (
                  <div
                    key={label}
                    className={cn(
                      "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-xs",
                      "active" in rest ? "bg-cyan-400/10 text-cyan-200" : "text-slate-500",
                    )}
                  >
                    <Icon className="h-3.5 w-3.5" />
                    {label}
                  </div>
                ))}
              </div>

              {/* Main */}
              <div className="min-w-0 flex-1 space-y-5 p-4 sm:p-6">
                <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                  {SUMMARY.map((item) => (
                    <div
                      key={item.label}
                      className="rounded-lg border border-white/[0.07] bg-white/[0.02] p-3.5"
                    >
                      <p className="text-[11px] text-slate-500">{item.label}</p>
                      <p className="mt-1 text-2xl font-semibold tracking-tight text-white">
                        {item.value}
                      </p>
                      <p className="mt-0.5 text-[11px] text-slate-500">{item.note}</p>
                    </div>
                  ))}
                </div>

                <div className="grid gap-5 xl:grid-cols-[1.6fr_1fr]">
                  {/* Calls table */}
                  <div className="overflow-hidden rounded-lg border border-white/[0.07]">
                    <div className="flex items-center justify-between border-b border-white/[0.07] px-4 py-3">
                      <p className="text-xs font-medium text-white">Recent calls</p>
                      <p className="text-[11px] text-slate-500">Tonight</p>
                    </div>
                    <div className="divide-y divide-white/[0.05]">
                      {CALLS.map((call) => (
                        <div
                          key={call.time}
                          className="grid grid-cols-[3.5rem_1fr_auto] items-center gap-3 px-4 py-2.5 sm:grid-cols-[4rem_6rem_1fr_auto]"
                        >
                          <span className="font-mono text-[11px] text-slate-500">{call.time}</span>
                          <span className="hidden truncate text-xs text-slate-200 sm:block">
                            {call.caller}
                          </span>
                          <span className="truncate text-xs text-slate-400">{call.issue}</span>
                          <span
                            className={cn(
                              "whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-medium",
                              TONE_CLASS[call.tone],
                            )}
                          >
                            {call.outcome}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Side cards */}
                  <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-1">
                    <div className="rounded-lg border border-rose-400/20 bg-rose-400/[0.04] p-4">
                      <div className="flex items-center justify-between">
                        <p className="text-xs font-medium text-rose-200">Emergency ticket #1042</p>
                        <span className="rounded-full bg-rose-400/15 px-2 py-0.5 text-[10px] text-rose-200">
                          Open
                        </span>
                      </div>
                      <dl className="mt-3 space-y-1.5 text-[11px]">
                        <Row label="Issue" value="Furnace failure, no heat" />
                        <Row label="Customer" value="Dana R. · returning" />
                        <Row label="Address" value="128 Example St." />
                        <Row label="Callback" value="(555) 010-0000" />
                      </dl>
                    </div>
                    <div className="rounded-lg border border-white/[0.07] bg-white/[0.02] p-4">
                      <div className="flex items-center justify-between">
                        <p className="text-xs font-medium text-white">Next appointment</p>
                        <span className="rounded-full bg-emerald-400/15 px-2 py-0.5 text-[10px] text-emerald-200">
                          Scheduled
                        </span>
                      </div>
                      <dl className="mt-3 space-y-1.5 text-[11px]">
                        <Row label="Service" value="Water heater inspection" />
                        <Row label="When" value="Thu · 10:00 – 11:00 AM" />
                        <Row label="Customer" value="Marcus T. · new" />
                      </dl>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </Reveal>

        <p className="mt-5 text-sm text-slate-500">
          Mockup for illustration only — not connected to a live account. Names, numbers and
          addresses are fictional.
        </p>
      </Container>
    </section>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-slate-500">{label}</dt>
      <dd className="truncate text-right text-slate-300">{value}</dd>
    </div>
  );
}
