import type { Metadata } from "next";

import { LegalPage, type LegalSection } from "@/components/marketing/legal-page";

export const metadata: Metadata = {
  title: "Privacy Policy — ESSR",
  description: "How ESSR handles personal information. Draft pending legal review.",
};

// Source of truth: docs/legal/PRIVACY_POLICY_DRAFT.md. Keep the two in step.
const SECTIONS: readonly LegalSection[] = [
  {
    heading: "Who we are",
    paragraphs: [
      "ESSR is operated by [LEGAL ENTITY NAME], [REGISTERED ADDRESS], India (\"we\", \"us\"). Contact: [PRIVACY CONTACT EMAIL].",
      "This policy covers this website and the ESSR service. When ESSR answers calls for a business that uses it, that business decides why caller information is collected and is responsible for it; we process it on the business's behalf under our agreement with them.",
    ],
  },
  {
    heading: "Information from this website",
    paragraphs: [
      "If you request early access we store your name, business name, work email, business type, and the date and version of the consent you gave. We use it only to contact you about early access to ESSR. We do not sell it or use it for advertising.",
      "We keep early-access requests for up to [24] months after our last contact with you, then delete them. You can ask us to delete them sooner at any time.",
    ],
  },
  {
    heading: "Information from calls ESSR handles",
    paragraphs: [
      "When ESSR answers a call for a business, it processes what the caller says (as a transcript), the number the call came from, and details the caller gives, such as their name, callback number and service address. Calls may be recorded by our telephony provider; callers are told the call is recorded, as configured by the business.",
      "This information is used to handle the caller's request — for example booking an appointment or alerting the business about an emergency — and is available only to that business's authorized staff and to us for operating and supporting the service.",
      "Recordings are held by our voice provider for [14] days. Transcripts and call records are kept for [PERIOD AGREED WITH EACH BUSINESS] and then deleted, unless the business deletes them sooner.",
    ],
  },
  {
    heading: "Service providers",
    paragraphs: [
      "We use service providers to run ESSR, including providers for voice calls, speech-to-text, AI language processing, hosting and email. They process information only to provide their services to us. Some are located outside India, including in the United States. The current list is available on request at [PRIVACY CONTACT EMAIL].",
    ],
  },
  {
    heading: "Security",
    paragraphs: [
      "We use access controls, encryption in transit, separation of each business's data, and limited staff access. No system is perfectly secure; we will notify affected businesses of a security incident as required by law and our agreements.",
    ],
  },
  {
    heading: "Your choices",
    paragraphs: [
      "You can ask to see, correct or delete personal information we hold about you by emailing [PRIVACY CONTACT EMAIL]. If your information came from a call to a business that uses ESSR, we may refer your request to that business.",
      "Depending on where you live you may have further rights under laws such as India's Digital Personal Data Protection Act, 2023, the GDPR or US state privacy laws. [JURISDICTION-SPECIFIC TEXT TO BE CONFIRMED BY COUNSEL.]",
    ],
  },
  {
    heading: "Changes",
    paragraphs: [
      "We will update this page when our practices change and show the date of the latest version above.",
    ],
  },
];

export default function PrivacyPage() {
  return <LegalPage title="Privacy Policy" updated="8 November 2026" sections={SECTIONS} />;
}
