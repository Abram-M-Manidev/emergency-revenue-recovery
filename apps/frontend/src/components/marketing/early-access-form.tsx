"use client";

import { useState } from "react";
import Link from "next/link";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";

import { API_BASE_URL } from "@/lib/constants";
import { cn } from "@/lib/utils/cn";

import { ctaPrimary, focusRing } from "./primitives";

const BUSINESS_TYPES = ["HVAC", "Plumbing", "Electrical", "Multi-trade", "Other"] as const;

/**
 * The version of the consent wording below. Must match
 * `CURRENT_CONSENT_VERSION` in apps/api/app/application/services/lead_service.py;
 * bump both whenever the wording changes.
 */
export const CONSENT_VERSION = "2026-11-08";

const earlyAccessSchema = z.object({
  name: z.string().trim().min(1, "Enter your name.").max(120, "Use 120 characters or fewer."),
  businessName: z
    .string()
    .trim()
    .min(1, "Enter your business name.")
    .max(160, "Use 160 characters or fewer."),
  email: z.string().trim().min(1, "Enter your work email.").email("Enter a valid email address."),
  businessType: z.enum(BUSINESS_TYPES, {
    errorMap: () => ({ message: "Choose your business type." }),
  }),
  consent: z.literal(true, {
    errorMap: () => ({ message: "Please agree so we can contact you about early access." }),
  }),
  // Hidden from people; anything typed here marks the submission as automated.
  website: z.string().optional(),
});

type EarlyAccessValues = z.infer<typeof earlyAccessSchema>;

type SubmitState = "idle" | "submitting" | "sent" | "rate-limited" | "error";

/**
 * Sends an early-access request to the API (`POST /public/early-access`),
 * which stores it with the consent version. The API answers the same way for
 * a new and a repeat address, so this form never learns — or shows — whether
 * someone was already on the list.
 */
export function EarlyAccessForm() {
  const [state, setState] = useState<SubmitState>("idle");
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<EarlyAccessValues>({
    resolver: zodResolver(earlyAccessSchema),
    defaultValues: { name: "", businessName: "", email: "", website: "" },
  });

  const onSubmit = async (values: EarlyAccessValues) => {
    setState("submitting");
    try {
      const response = await fetch(`${API_BASE_URL}/public/early-access`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: values.name,
          business_name: values.businessName,
          email: values.email,
          business_type: values.businessType,
          consent: values.consent,
          consent_version: CONSENT_VERSION,
          website: values.website || null,
        }),
      });
      if (response.status === 202) {
        setState("sent");
        reset();
      } else if (response.status === 429) {
        setState("rate-limited");
      } else {
        setState("error");
      }
    } catch {
      setState("error");
    }
  };

  return (
    <form
      noValidate
      onSubmit={handleSubmit(onSubmit)}
      className="relative rounded-2xl border border-white/10 bg-[#0a101e] p-6 shadow-2xl shadow-black/40 sm:p-8"
    >
      <div className="grid gap-5 sm:grid-cols-2">
        <Field id="ea-name" label="Name" error={errors.name?.message}>
          <input
            id="ea-name"
            type="text"
            autoComplete="name"
            maxLength={120}
            aria-invalid={errors.name ? true : undefined}
            aria-describedby={errors.name ? "ea-name-error" : undefined}
            className={inputClass(!!errors.name)}
            {...register("name")}
          />
        </Field>
        <Field id="ea-business" label="Business name" error={errors.businessName?.message}>
          <input
            id="ea-business"
            type="text"
            autoComplete="organization"
            maxLength={160}
            aria-invalid={errors.businessName ? true : undefined}
            aria-describedby={errors.businessName ? "ea-business-error" : undefined}
            className={inputClass(!!errors.businessName)}
            {...register("businessName")}
          />
        </Field>
        <Field id="ea-email" label="Work email" error={errors.email?.message}>
          <input
            id="ea-email"
            type="email"
            autoComplete="email"
            maxLength={254}
            aria-invalid={errors.email ? true : undefined}
            aria-describedby={errors.email ? "ea-email-error" : undefined}
            className={inputClass(!!errors.email)}
            {...register("email")}
          />
        </Field>
        <Field id="ea-type" label="Business type" error={errors.businessType?.message}>
          <select
            id="ea-type"
            defaultValue=""
            aria-invalid={errors.businessType ? true : undefined}
            aria-describedby={errors.businessType ? "ea-type-error" : undefined}
            className={cn(inputClass(!!errors.businessType), "appearance-none")}
            {...register("businessType")}
          >
            <option value="" disabled>
              Select…
            </option>
            {BUSINESS_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </Field>
      </div>

      {/* Honeypot: off-screen and skipped by keyboard and screen readers. */}
      <div aria-hidden="true" className="absolute -left-[9999px] h-px w-px overflow-hidden">
        <label htmlFor="ea-website">Website</label>
        <input id="ea-website" type="text" tabIndex={-1} autoComplete="off" {...register("website")} />
      </div>

      <div className="mt-6 space-y-2">
        <label htmlFor="ea-consent" className="flex items-start gap-3 text-sm text-slate-300">
          <input
            id="ea-consent"
            type="checkbox"
            aria-invalid={errors.consent ? true : undefined}
            aria-describedby={errors.consent ? "ea-consent-error" : undefined}
            className={cn("mt-0.5 h-4 w-4 shrink-0 rounded border-white/20 bg-[#060912]", focusRing)}
            {...register("consent")}
          />
          <span>
            I agree that ESSR may store these details and email me about early access. I can ask
            for them to be deleted at any time. See the{" "}
            <Link href="/privacy" className={cn("rounded text-cyan-300 underline", focusRing)}>
              Privacy Policy
            </Link>
            .
          </span>
        </label>
        {errors.consent ? (
          <p id="ea-consent-error" className="text-xs text-rose-300">
            {errors.consent.message}
          </p>
        ) : null}
      </div>

      <button
        type="submit"
        disabled={state === "submitting"}
        className={cn(ctaPrimary, "mt-7 w-full disabled:cursor-wait disabled:opacity-70")}
      >
        {state === "submitting" ? "Sending…" : "Request Early Access"}
      </button>

      <div aria-live="polite" className="mt-4 min-h-[1.25rem] text-sm leading-relaxed">
        {state === "sent" ? (
          <p className="text-slate-300">
            Thanks — your request was received. We&apos;ll email you about fit and next steps.
          </p>
        ) : null}
        {state === "rate-limited" ? (
          <p className="text-amber-200">Too many attempts. Please wait a minute and try again.</p>
        ) : null}
        {state === "error" ? (
          <p className="text-rose-300">
            Something went wrong and your request was not sent. Please try again.
          </p>
        ) : null}
      </div>
    </form>
  );
}

function inputClass(invalid: boolean) {
  return cn(
    "h-11 w-full rounded-lg border bg-[#060912] px-3.5 text-sm text-white placeholder:text-slate-600 transition-colors",
    invalid ? "border-rose-400/60" : "border-white/10 hover:border-white/20",
    focusRing,
  );
}

function Field({
  id,
  label,
  error,
  children,
}: {
  id: string;
  label: string;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-2">
      <label htmlFor={id} className="block text-sm font-medium text-slate-200">
        {label}{" "}
        <span className="text-rose-300" aria-hidden="true">
          *
        </span>
        <span className="sr-only"> (required)</span>
      </label>
      {children}
      {error ? (
        <p id={`${id}-error`} className="text-xs text-rose-300">
          {error}
        </p>
      ) : null}
    </div>
  );
}
