// Plain module (no "use client") so both the client header and the server
// footer can import the same list. Bare hashes on purpose: the header and
// footer only render on the landing page, and native anchor scrolling
// honours each section's scroll-margin (next/link overshoots upward).
export const NAV_LINKS = [
  { href: "#product", label: "Product" },
  { href: "#how-it-works", label: "How It Works" },
  { href: "#features", label: "Features" },
  { href: "#faq", label: "FAQ" },
] as const;
