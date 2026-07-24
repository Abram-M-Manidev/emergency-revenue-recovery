"use client";

import { EmergencyKeywordsSection } from "@/components/business-knowledge/emergency-keywords-section";
import { FAQsSection } from "@/components/business-knowledge/faqs-section";
import { HoursSection } from "@/components/business-knowledge/hours-section";
import { ProfileSection } from "@/components/business-knowledge/profile-section";
import { ServiceAreasSection } from "@/components/business-knowledge/service-areas-section";
import { ServicesSection } from "@/components/business-knowledge/services-section";

export default function BusinessKnowledgePage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Business Knowledge</h1>
        <p className="text-sm text-muted-foreground">
          The verified information your AI will use to answer calls — never hardcoded.
        </p>
      </div>

      <ProfileSection />
      <HoursSection />
      <ServiceAreasSection />
      <ServicesSection />
      <EmergencyKeywordsSection />
      <FAQsSection />
    </div>
  );
}
