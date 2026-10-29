import { CalendarCheck, PhoneIncoming, ShieldAlert, Sparkles } from "lucide-react";

import { SampleDataBadge } from "./primitives";

const TRANSCRIPT = [
  { who: "Caller", text: "Our furnace stopped and it's getting really cold in here." },
  {
    who: "ESSR",
    text: "I'm sorry to hear that. Is anyone in the home at risk from the cold right now?",
  },
  { who: "Caller", text: "My mother's here — she's 82." },
] as const;

/** Hero illustration: one fictional after-hours call moving through intake, classification and action. */
export function CallFlowVisual() {
  return (
    <figure className="relative" aria-label="Illustration of an after-hours call handled by ESSR">
      <div className="relative rounded-2xl border border-white/10 bg-gradient-to-b from-[#0d1528] to-[#090f1d] p-1.5 shadow-2xl shadow-black/50">
        <div className="rounded-xl border border-white/[0.06] bg-[#0a1120]">
          {/* Call header */}
          <div className="flex items-center justify-between border-b border-white/[0.06] px-5 py-4">
            <div className="flex items-center gap-3">
              <span className="relative flex h-9 w-9 items-center justify-center rounded-full bg-cyan-400/10 text-cyan-300">
                <PhoneIncoming className="h-4 w-4" aria-hidden="true" />
                <span className="absolute inset-0 rounded-full ring-1 ring-cyan-300/40 [animation-duration:2.4s] motion-safe:animate-ping" />
              </span>
              <div>
                <p className="text-sm font-medium text-white">Incoming call · 9:47 PM</p>
                <p className="text-xs text-slate-500">After hours · Main line</p>
              </div>
            </div>
            <Waveform />
          </div>

          {/* Transcript */}
          <div className="space-y-3 px-5 py-5">
            {TRANSCRIPT.map((line, index) => (
              <div
                key={index}
                className={line.who === "ESSR" ? "flex justify-end" : "flex justify-start"}
              >
                <p
                  className={
                    line.who === "ESSR"
                      ? "max-w-[85%] rounded-lg rounded-br-sm bg-cyan-400/10 px-3 py-2 text-[13px] leading-snug text-cyan-50"
                      : "max-w-[85%] rounded-lg rounded-bl-sm bg-white/[0.05] px-3 py-2 text-[13px] leading-snug text-slate-200"
                  }
                >
                  <span className="sr-only">{line.who}: </span>
                  {line.text}
                </p>
              </div>
            ))}
          </div>

          {/* Classification + actions */}
          <div className="grid gap-3 border-t border-white/[0.06] px-5 py-5 sm:grid-cols-2">
            <div className="rounded-lg border border-rose-400/20 bg-rose-400/[0.06] p-3">
              <p className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-rose-300">
                <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
                Classified: Emergency
              </p>
              <p className="mt-1.5 text-xs leading-relaxed text-slate-400">
                Matched rule “No heat + vulnerable occupant”
              </p>
            </div>
            <div className="rounded-lg border border-white/[0.08] bg-white/[0.03] p-3">
              <p className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-cyan-300">
                <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                Action taken
              </p>
              <p className="mt-1.5 text-xs leading-relaxed text-slate-400">
                Emergency ticket #1042 created with address and callback number
              </p>
            </div>
          </div>
        </div>
      </div>

      {/* Secondary card: a routine request that ends in a booking. */}
      <div className="relative -mt-6 ml-auto mr-4 w-[78%] max-w-sm rounded-xl border border-white/10 bg-[#0d1528]/95 p-4 shadow-xl shadow-black/40 backdrop-blur sm:mr-8">
        <div className="flex items-start gap-3">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-emerald-400/10 text-emerald-300">
            <CalendarCheck className="h-4 w-4" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <p className="text-sm font-medium text-white">Appointment booked</p>
            <p className="mt-0.5 text-xs text-slate-400">
              Water heater inspection · Thu 10:00 AM · time confirmed by caller
            </p>
          </div>
        </div>
      </div>

      <figcaption className="mt-4 flex justify-end pr-4 sm:pr-8">
        <SampleDataBadge />
      </figcaption>
    </figure>
  );
}

function Waveform() {
  const bars = [6, 12, 18, 10, 16, 8, 14, 6];
  return (
    <div className="flex h-5 items-center gap-[3px]" aria-hidden="true">
      {bars.map((height, index) => (
        <span
          key={index}
          className="w-[3px] origin-center rounded-full bg-cyan-300/70 motion-safe:animate-[wave_1.2s_ease-in-out_infinite]"
          style={{ height, animationDelay: `${index * 0.1}s` }}
        />
      ))}
    </div>
  );
}
