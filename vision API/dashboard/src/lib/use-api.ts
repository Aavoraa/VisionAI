"use client";

import { useCallback, useEffect, useEffectEvent, useState } from "react";

type Settled<T> = { requestId: string } & ({ ok: true; data: T } | { ok: false; error: Error });

export type ApiQuery<T> = (
  | { status: "loading"; data: null; error: null }
  | { status: "success"; data: T; error: null }
  | { status: "error"; data: null; error: Error }
) & {
  /** True while newer data is being fetched and the previous result is still shown. */
  refreshing: boolean;
  reload: () => void;
};

/**
 * Load data on mount, whenever `key` changes, and on `reload()`.
 *
 * State is only set when a request settles (never synchronously in the effect), and results of
 * superseded requests are dropped, so quick key changes cannot show out-of-order data. While a
 * new request is in flight the last successful data stays visible (`refreshing: true`).
 */
export function useApiQuery<T>(load: () => Promise<T>, key: string = ""): ApiQuery<T> {
  const [version, setVersion] = useState(0);
  const [settled, setSettled] = useState<Settled<T> | null>(null);
  const requestId = `${key}\u0000${version}`;
  const fetchData = useEffectEvent(load);

  useEffect(() => {
    let active = true;
    fetchData().then(
      (data) => active && setSettled({ requestId, ok: true, data }),
      (error: unknown) =>
        active &&
        setSettled({ requestId, ok: false, error: error instanceof Error ? error : new Error("Unexpected error") }),
    );
    return () => {
      active = false;
    };
  }, [requestId]);

  const reload = useCallback(() => setVersion((current) => current + 1), []);
  const current = settled?.requestId === requestId;

  if (settled?.ok) {
    return { status: "success", data: settled.data, error: null, refreshing: !current, reload };
  }
  if (settled && !settled.ok && current) {
    return { status: "error", data: null, error: settled.error, refreshing: false, reload };
  }
  return { status: "loading", data: null, error: null, refreshing: false, reload };
}
