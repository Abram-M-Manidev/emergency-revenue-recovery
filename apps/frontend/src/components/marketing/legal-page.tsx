import { SiteFooter } from "./site-footer";
import { SiteHeader } from "./site-header";
import { Container } from "./primitives";

export type LegalSection = { heading: string; paragraphs: readonly string[] };

/**
 * Shared layout for the public Privacy Policy and Terms pages. The text is a
 * DRAFT pending review by a qualified lawyer, and the page says so: nothing
 * here may be presented as reviewed or as a compliance claim until it is.
 * Bracketed items are facts only the operator can supply.
 */
export function LegalPage({
  title,
  updated,
  sections,
}: {
  title: string;
  updated: string;
  sections: readonly LegalSection[];
}) {
  return (
    <div className="min-h-screen bg-[#060912] text-slate-200">
      <SiteHeader />
      <main id="main">
        <Container className="max-w-3xl py-20 sm:py-28">
          <h1 className="text-3xl font-semibold tracking-tight text-white sm:text-4xl">{title}</h1>
          <p className="mt-3 text-sm text-slate-500">Last updated {updated}</p>
          <p
            role="note"
            className="mt-6 rounded-lg border border-amber-300/25 bg-amber-300/[0.07] p-3 text-sm leading-relaxed text-amber-100"
          >
            Draft pending legal review. Items in [brackets] will be completed before ESSR is offered
            to customers.
          </p>
          <div className="mt-10 space-y-10">
            {sections.map((section) => (
              <section key={section.heading} className="space-y-3">
                <h2 className="text-lg font-semibold text-white">{section.heading}</h2>
                {section.paragraphs.map((paragraph) => (
                  <p key={paragraph} className="text-sm leading-relaxed text-slate-400">
                    {paragraph}
                  </p>
                ))}
              </section>
            ))}
          </div>
        </Container>
      </main>
      <SiteFooter />
    </div>
  );
}
