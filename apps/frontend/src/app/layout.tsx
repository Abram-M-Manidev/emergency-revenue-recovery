import type { Metadata } from "next";
import { Inter } from "next/font/google";

import { AuthProvider } from "@/components/providers/auth-provider";
import { ToastProvider } from "@/components/providers/toast-provider";
import { API_BASE_URL } from "@/lib/constants";

import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-sans" });

export const metadata: Metadata = {
  // Pages set their own titles; this is the fallback and the template.
  title: { default: "ESSR", template: "%s" },
  description:
    "ESSR answers calls for HVAC, plumbing and electrical businesses, books agreed appointments and records emergencies for your team.",
  // Production serves the site and API from one origin behind Caddy, so the
  // API base URL's origin is the site's unless NEXT_PUBLIC_SITE_URL says otherwise.
  metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_URL ?? new URL(API_BASE_URL).origin),
  openGraph: { siteName: "ESSR", type: "website" },
  robots: { index: true, follow: true },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${inter.variable} font-sans`}>
        <ToastProvider>
          <AuthProvider>{children}</AuthProvider>
        </ToastProvider>
      </body>
    </html>
  );
}
