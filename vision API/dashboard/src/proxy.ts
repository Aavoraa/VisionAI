import { NextResponse, type NextRequest } from "next/server";

export const SESSION_COOKIE = "vision_session";
const PUBLIC_PATHS = ["/login", "/signup"];

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((path) => pathname === path || pathname.startsWith(`${path}/`));
}

/**
 * Default-deny page guard: without a session cookie every app page redirects to sign-in.
 *
 * This is only a fast pre-check. The cookie is validated by the API on every request (the
 * app shell calls /v1/auth/me and signs out on 401), so a stale cookie cannot grant access.
 * Public pages never redirect based on the cookie, which avoids redirect loops when a cookie
 * exists but has expired.
 */
export function proxy(request: NextRequest): NextResponse {
  const { pathname, search } = request.nextUrl;
  if (isPublicPath(pathname) || request.cookies.has(SESSION_COOKIE)) {
    return NextResponse.next();
  }
  const url = request.nextUrl.clone();
  url.pathname = "/login";
  url.search = "";
  if (pathname !== "/") url.searchParams.set("next", `${pathname}${search}`);
  return NextResponse.redirect(url);
}

export const config = {
  // Everything except the API proxy, Next internals and static files.
  matcher: ["/((?!api/|_next/static|_next/image|favicon\\.ico|.*\\.[a-zA-Z0-9]+$).*)"],
};
