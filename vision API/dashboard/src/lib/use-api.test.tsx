import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useApiQuery } from "./use-api";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("useApiQuery", () => {
  it("goes from loading to success", async () => {
    const { result } = renderHook(() => useApiQuery(async () => 42));
    expect(result.current.status).toBe("loading");
    await waitFor(() => expect(result.current.status).toBe("success"));
    expect(result.current.data).toBe(42);
  });

  it("exposes errors and recovers on reload", async () => {
    let fail = true;
    const { result } = renderHook(() =>
      useApiQuery(async () => {
        if (fail) throw new Error("boom");
        return "ok";
      }),
    );
    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.error?.message).toBe("boom");

    fail = false;
    act(() => result.current.reload());
    expect(result.current.status).toBe("loading"); // retrying after an error shows loading
    await waitFor(() => expect(result.current.data).toBe("ok"));
  });

  it("keeps showing data while refreshing", async () => {
    const pending = deferred<string>();
    let calls = 0;
    const { result } = renderHook(() => useApiQuery(() => (++calls === 1 ? Promise.resolve("first") : pending.promise)));
    await waitFor(() => expect(result.current.data).toBe("first"));

    act(() => result.current.reload());
    expect(result.current).toMatchObject({ status: "success", data: "first", refreshing: true });
    await act(async () => pending.resolve("second"));
    expect(result.current).toMatchObject({ data: "second", refreshing: false });
  });

  it("drops responses from superseded requests", async () => {
    const slow = deferred<string>();
    const fast = deferred<string>();
    const { result, rerender } = renderHook(({ key }) => useApiQuery(() => (key === "a" ? slow.promise : fast.promise), key), {
      initialProps: { key: "a" },
    });
    rerender({ key: "b" });
    await act(async () => fast.resolve("b-data"));
    await act(async () => slow.resolve("a-data")); // arrives late, must be ignored
    expect(result.current.data).toBe("b-data");
  });
});
