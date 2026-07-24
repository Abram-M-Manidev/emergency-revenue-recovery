import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { AUTH_COOKIE_NAME, ROUTES } from "@/lib/constants";

export default async function RootPage() {
  const cookieStore = await cookies();
  const hasSession = cookieStore.has(AUTH_COOKIE_NAME);
  redirect(hasSession ? ROUTES.dashboard : ROUTES.login);
}
