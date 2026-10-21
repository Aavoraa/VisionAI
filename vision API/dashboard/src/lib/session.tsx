"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from "react";

import { ApiError, api, unwrap, type User } from "@/lib/api/client";
import { useApiQuery } from "@/lib/use-api";

type SessionContextValue = (
  | { status: "loading"; user: null; error: null }
  | { status: "authenticated"; user: User; error: null }
  | { status: "error"; user: null; error: string }
) & {
  refresh: () => void;
  signOut: () => Promise<void>;
};

const SessionContext = createContext<SessionContextValue | null>(null);

export function loginUrl(pathname: string | null): string {
  const next = pathname && pathname !== "/" ? `?next=${encodeURIComponent(pathname)}` : "";
  return `/login${next}`;
}

/** 401 (no/expired session) and 403 (deactivated account) mean "sign in again". */
export function isSignedOutError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}

/**
 * Loads the signed-in user once for the app shell (navigating between pages keeps it).
 * A missing, expired or deactivated session sends the user to the sign-in page.
 */
export function SessionProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const me = useApiQuery(() => unwrap(api.GET("/v1/auth/me")));
  const signedOut = me.status === "error" && isSignedOutError(me.error);

  useEffect(() => {
    if (signedOut) router.replace(loginUrl(pathname));
    // Only react to the sign-out itself, not to later navigation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signedOut, router]);

  const signOut = useCallback(async () => {
    try {
      await unwrap(api.POST("/v1/auth/logout"));
    } finally {
      router.replace("/login");
    }
  }, [router]);

  const value = useMemo<SessionContextValue>(() => {
    const base = { refresh: me.reload, signOut };
    if (me.status === "success") return { status: "authenticated", user: me.data, error: null, ...base };
    if (me.status === "error" && !signedOut) return { status: "error", user: null, error: me.error.message, ...base };
    return { status: "loading", user: null, error: null, ...base }; // includes "redirecting to sign-in"
  }, [me, signedOut, signOut]);

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside <SessionProvider>");
  return value;
}

/** For pages rendered inside the authenticated shell, where the user is guaranteed. */
export function useCurrentUser(): User {
  const session = useSession();
  if (session.status !== "authenticated") throw new Error("useCurrentUser requires an authenticated session");
  return session.user;
}
