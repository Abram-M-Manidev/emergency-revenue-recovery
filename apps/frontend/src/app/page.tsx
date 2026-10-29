import type { Metadata } from "next";

import { AudienceSection } from "@/components/marketing/audience-section";
import { EarlyAccessSection } from "@/components/marketing/early-access-section";
import { FaqSection } from "@/components/marketing/faq-section";
import { FeaturesSection } from "@/components/marketing/features-section";
import { Hero } from "@/components/marketing/hero";
import { HowItWorks } from "@/components/marketing/how-it-works";
import { ProblemSection } from "@/components/marketing/problem-section";
import { ProductPreview } from "@/components/marketing/product-preview";
import { SiteFooter } from "@/components/marketing/site-footer";
import { SiteHeader } from "@/components/marketing/site-header";

export const metadata: Metadata = {
  title: "ESSR — AI call handling that recovers missed jobs",
  description:
    "ESSR answers incoming calls for HVAC, plumbing and electrical businesses, books agreed appointments and turns emergencies into dispatch tickets.",
};

/**
 * Public marketing page. Signed-in users never reach it: middleware.ts
 * redirects `/` to the dashboard when a session cookie is present.
 */
export default function LandingPage() {
  return (
    <div className="min-h-screen bg-[#060912] text-slate-200 selection:bg-cyan-300/30">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-cyan-300 px-4 py-2 text-sm font-semibold text-[#04121a] focus:not-sr-only focus:fixed focus:left-4 focus:top-4"
      >
        Skip to content
      </a>
      <SiteHeader />
      <main id="main">
        <Hero />
        <ProblemSection />
        <HowItWorks />
        <FeaturesSection />
        <ProductPreview />
        <AudienceSection />
        <FaqSection />
        <EarlyAccessSection />
      </main>
      <SiteFooter />
    </div>
  );
}
