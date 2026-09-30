"use client";

/**
 * The page an emergency page's acknowledgement link opens. No sign-in: the
 * signed token in the link is the authority, and it can acknowledge only the
 * one page it was issued for.
 *
 * The token arrives in the URL fragment (`/ack#<token>`), which browsers never
 * send to a server — so it stays out of access logs and Referer headers. And
 * nothing is acknowledged on load: messaging apps fetch links to build
 * previews, so acknowledging requires pressing the button.
 */

import { useEffect, useState } from "react";
import { Siren } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError } from "@/lib/api/client";
import { acknowledgeWithLink } from "@/lib/api/paging";

type State =
  | { kind: "ready" }
  | { kind: "missing" }
  | { kind: "done"; alreadyAcknowledged: boolean }
  | { kind: "invalid" }
  | { kind: "error" };

export default function AcknowledgePage() {
  const [token, setToken] = useState<string | null>(null);
  const [state, setState] = useState<State>({ kind: "ready" });
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    const fromFragment = window.location.hash.replace(/^#/, "").trim();
    if (fromFragment) {
      setToken(fromFragment);
    } else {
      setState({ kind: "missing" });
    }
  }, []);

  async function acknowledge() {
    if (!token) return;
    setIsSubmitting(true);
    try {
      const result = await acknowledgeWithLink(token);
      setState({ kind: "done", alreadyAcknowledged: result.already_acknowledged });
    } catch (error) {
      setState({ kind: error instanceof ApiError && error.status === 404 ? "invalid" : "error" });
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-8 bg-muted/30 p-4">
      <div className="flex items-center gap-2">
        <Siren className="h-6 w-6 text-primary" aria-hidden="true" />
        <span className="text-base font-semibold tracking-tight">Emergency page</span>
      </div>
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-lg">
            {state.kind === "done" ? "Acknowledged" : "Acknowledge this emergency"}
          </CardTitle>
          <CardDescription>
            {state.kind === "done"
              ? state.alreadyAcknowledged
                ? "This emergency had already been acknowledged. Nothing else was changed."
                : "Thank you. Escalation has stopped and nobody else will be paged about this emergency. Contact the caller using the details in your text message."
              : state.kind === "missing"
                ? "This link is incomplete. Open it again from the text message, or acknowledge in the Dispatch dashboard."
                : state.kind === "invalid"
                  ? "This link is invalid or has expired. Acknowledge the emergency in the Dispatch dashboard instead."
                  : state.kind === "error"
                    ? "Something went wrong and the emergency was NOT acknowledged. Try again, or use the Dispatch dashboard."
                    : "Pressing the button tells the business you have taken this emergency. Until someone does, the next on-call person will be paged."}
          </CardDescription>
        </CardHeader>
        {state.kind === "ready" || state.kind === "error" ? (
          <CardContent>
            <Button className="w-full" onClick={acknowledge} isLoading={isSubmitting} disabled={!token}>
              I have it — acknowledge
            </Button>
          </CardContent>
        ) : null}
      </Card>
    </div>
  );
}
