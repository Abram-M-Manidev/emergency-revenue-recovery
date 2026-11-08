import type { Metadata } from "next";

import { LegalPage, type LegalSection } from "@/components/marketing/legal-page";

export const metadata: Metadata = {
  title: "Terms of Service — ESSR",
  description: "Terms for using the ESSR website and service. Draft pending legal review.",
};

// Source of truth: docs/legal/TERMS_OF_SERVICE_DRAFT.md. Keep the two in step.
const SECTIONS: readonly LegalSection[] = [
  {
    heading: "The service",
    paragraphs: [
      "ESSR is an AI phone assistant for service businesses, operated by [LEGAL ENTITY NAME]. It answers calls, collects caller details, offers and books appointment times from the business's own schedule, records emergency requests as tickets, and can alert the business's chosen contacts. Use of the service by a business is governed by a separate written agreement, which takes precedence over these terms.",
    ],
  },
  {
    heading: "Not an emergency service",
    paragraphs: [
      "ESSR is not a substitute for emergency services. It cannot contact police, fire, ambulance or gas emergency lines, and it does not guarantee that a technician will attend. When a caller reports a hazard such as a gas smell, smoke or a carbon monoxide alarm, ESSR tells them to get to safety and call the emergency number. Alerts to a business depend on the channels the business configures and on third-party networks, and may be delayed or fail.",
    ],
  },
  {
    heading: "AI limitations",
    paragraphs: [
      "ESSR uses automated speech recognition and AI. It can mishear, misunderstand or make mistakes. Businesses remain responsible for reviewing tickets and appointments, for their own services and pricing, and for how they respond to their customers.",
    ],
  },
  {
    heading: "Website",
    paragraphs: [
      "Information on this website describes the service as it currently is and may change. We do not promise any particular increase in bookings or revenue.",
    ],
  },
  {
    heading: "Liability, law and contact",
    paragraphs: [
      "[LIMITATION OF LIABILITY, GOVERNING LAW AND DISPUTE TERMS TO BE DRAFTED BY COUNSEL.]",
      "Questions: [CONTACT EMAIL].",
    ],
  },
];

export default function TermsPage() {
  return <LegalPage title="Terms of Service" updated="8 November 2026" sections={SECTIONS} />;
}
