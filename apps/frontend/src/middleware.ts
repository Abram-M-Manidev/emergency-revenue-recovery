import { NextResponse, type NextRequest } from "next/server";

import { AUTH_COOKIE_NAME, ROUTES } from "@/lib/constants";

const PROTECTED_PREFIXES = [ROUTES.dashboard, ROUTES.settings];
const AUTH_PAGES = ["/login", "/register"];

/**
 * Edge-level gate based on the presence of the httpOnly refresh cookie —
 * NOT full JWT verification (that would require sharing the JWT secret
 * with the edge runtime). It only prevents obviously-unauthenticated users
 * from reaching protected pages; every real API call is still authorized
 * independently by the backend.
 */
export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const hasSession = request.cookies.has(AUTH_COOKIE_NAME);

  if (pathname === "/") {
    return NextResponse.redirect(new URL(hasSession ? ROUTES.dashboard : ROUTES.login, request.url));
  }

  const isProtected = PROTECTED_PREFIXES.some((prefix) => pathname.startsWith(prefix));
  if (isProtected && !hasSession) {
    const loginUrl = new URL(ROUTES.login, request.url);
    loginUrl.searchParams.set("from", pathname);
    return NextResponse.redirect(loginUrl);
  }

  const isAuthPage = AUTH_PAGES.some((page) => pathname.startsWith(page));
  if (isAuthPage && hasSession) {
    return NextResponse.redirect(new URL(ROUTES.dashboard, request.url));
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/", "/login", "/register", "/dashboard/:path*", "/settings/:path*"],
};
