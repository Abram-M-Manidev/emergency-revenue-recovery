"use client";

import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Info } from "lucide-react";

import { cn } from "@/lib/utils/cn";

import { ctaPrimary, focusRing } from "./primitives";

const BUSINESS_TYPES = ["HVAC", "Plumbing", "Electrical", "Multi-trade", "Other"] as const;

const earlyAccessSchema = z.object({
  name: z.string().trim().min(1, "Enter your name."),
  businessName: z.string().trim().min(1, "Enter your business name."),
  email: z.string().trim().min(1, "Enter your work email.").email("Enter a valid email address."),
  businessType: z.enum(BUSINESS_TYPES, {
    errorMap: () => ({ message: "Choose your business type." }),
  }),
});

type EarlyAccessValues = z.infer<typeof earlyAccessSchema>;

/**
 * Frontend prototype. There is no early-access endpoint in the API yet, so
 * a valid submission is validated and then discarded: nothing is sent,
 * stored or logged. Connecting it needs a backend endpoint (or an
 * explicitly configured form service) plus a privacy notice.
 */
export function EarlyAccessForm() {
  const [prototypeNotice, setPrototypeNotice] = useState(false);
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<EarlyAccessValues>({
    resolver: zodResolver(earlyAccessSchema),
    defaultValues: { name: "", businessName: "", email: "" },
  });

  const onSubmit = () => {
    setPrototypeNotice(true);
  };

  return (
    <form
      noValidate
      onSubmit={handleSubmit(onSubmit)}
      aria-describedby="early-access-prototype-note"
      className="rounded-2xl border border-white/10 bg-[#0a101e] p-6 shadow-2xl shadow-black/40 sm:p-8"
    >
      <p
        id="early-access-prototype-note"
        className="mb-6 flex gap-2.5 rounded-lg border border-amber-300/25 bg-amber-300/[0.07] p-3 text-xs leading-relaxed text-amber-100"
      >
        <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        Preview form: submissions are not sent or stored yet. Online sign-up will open once the
        early-access list is connected.
      </p>

      <div className="grid gap-5 sm:grid-cols-2">
        <Field id="ea-name" label="Name" error={errors.name?.message}>
          <input
            id="ea-name"
            type="text"
            autoComplete="name"
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

      <button type="submit" className={cn(ctaPrimary, "mt-7 w-full")}>
        Get Early Access
      </button>

      <div aria-live="polite" className="mt-4 min-h-[1.25rem]">
        {prototypeNotice ? (
          <p className="text-sm leading-relaxed text-slate-300">
            Thanks — your details look complete. Because this form isn&apos;t connected yet,{" "}
            <span className="font-medium text-white">nothing was sent or saved</span>, and we
            won&apos;t be able to contact you from it.
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
