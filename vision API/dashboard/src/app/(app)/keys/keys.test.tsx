import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ GET: vi.fn(), POST: vi.fn(), PATCH: vi.fn(), DELETE: vi.fn() }));
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...original, api };
});

import { EXPIRY_OPTIONS, keyStatus, validateKeyName } from "@/lib/keys";
import { apiErrorResponse, apiResponse } from "@/test-utils";

import KeysPage from "./page";

const NOW = new Date("2026-09-24T12:00:00Z");
const KEY = {
  id: "k1",
  name: "production",
  prefix: "sk_live_AbCd",
  is_active: true,
  expires_at: null,
  last_used_at: "2026-09-24T10:00:00Z",
  created_at: "2026-09-01T08:00:00Z",
};
const REVOKED = { ...KEY, id: "k2", name: "old laptop", prefix: "sk_live_EfGh", is_active: false, last_used_at: null };
const EXPIRED = { ...KEY, id: "k3", name: "temp", prefix: "sk_live_IjKl", expires_at: "2020-01-01T00:00:00Z" };
const SECRET = "sk_live_EXAMPLE_not_a_real_key_000001";

beforeEach(() => {
  Object.values(api).forEach((fn) => fn.mockReset());
});

function rows() {
  return screen.getAllByRole("row").slice(1); // skip the header row
}

describe("key helpers", () => {
  it("derives the status", () => {
    expect(keyStatus(KEY, NOW)).toBe("active");
    expect(keyStatus(REVOKED, NOW)).toBe("revoked");
    expect(keyStatus(EXPIRED, NOW)).toBe("expired");
    expect(keyStatus({ ...KEY, expires_at: "2026-12-01T00:00:00Z" }, NOW)).toBe("active");
  });

  it("validates names", () => {
    expect(validateKeyName("  ")).toMatch(/Give the key a name/);
    expect(validateKeyName("x".repeat(101))).toMatch(/at most 100/);
    expect(validateKeyName("ok")).toBeNull();
    expect(EXPIRY_OPTIONS.map((o) => o.days)).toEqual([null, 30, 90, 365]);
  });
});

describe("KeysPage", () => {
  it("lists keys with their status and hides revoke for inactive keys", async () => {
    api.GET.mockReturnValue(apiResponse(200, { items: [KEY, REVOKED, EXPIRED] }));
    render(<KeysPage />);

    await screen.findByText("production");
    const [active, revoked, expired] = rows();
    expect(within(active).getByText("Active")).toBeInTheDocument();
    expect(within(active).getByText("sk_live_AbCd…")).toBeInTheDocument();
    expect(within(active).getByRole("button", { name: "Revoke production" })).toBeInTheDocument();
    expect(within(revoked).getByText("Revoked")).toBeInTheDocument();
    expect(within(revoked).getAllByRole("cell")[2]).toHaveTextContent("Never"); // last used: never
    expect(within(revoked).queryByRole("button", { name: /Revoke/ })).not.toBeInTheDocument();
    expect(within(expired).getByText("Expired")).toBeInTheDocument();
    expect(api.GET).toHaveBeenCalledWith("/v1/dashboard/keys");
  });

  it("shows an empty state", async () => {
    api.GET.mockReturnValue(apiResponse(200, { items: [] }));
    render(<KeysPage />);
    expect(await screen.findByText("No API keys yet")).toBeInTheDocument();
  });

  it("creates a key and shows the secret exactly once", async () => {
    api.GET.mockReturnValueOnce(apiResponse(200, { items: [] })).mockReturnValue(apiResponse(200, { items: [KEY] }));
    api.POST.mockReturnValue(apiResponse(201, { key: KEY, secret: SECRET }));
    const user = userEvent.setup(); // installs a clipboard stub on navigator; spy on it afterwards
    const writeText = vi.spyOn(navigator.clipboard, "writeText");
    render(<KeysPage />);
    await screen.findByText("No API keys yet");

    await user.click(screen.getByRole("button", { name: "Create key" }));
    const dialog = screen.getByRole("dialog", { name: "Create API key" });
    expect(within(dialog).getByLabelText("Name")).toHaveFocus();
    await user.type(within(dialog).getByLabelText("Name"), "  production  ");
    await user.selectOptions(within(dialog).getByLabelText("Expires"), "90");
    await user.click(within(dialog).getByRole("button", { name: "Create key" }));

    const reveal = await screen.findByRole("dialog", { name: "Save your API key" });
    expect(api.POST).toHaveBeenCalledWith("/v1/dashboard/keys", { body: { name: "production", expires_in_days: 90 } });
    expect(within(reveal).getByTestId("new-secret")).toHaveTextContent(SECRET);
    expect(within(reveal).queryByRole("button", { name: "Close" })).not.toBeInTheDocument(); // must acknowledge
    expect(within(reveal).getByRole("button", { name: "I've saved it" })).toHaveFocus(); // focus not lost

    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Save your API key" })).toBeInTheDocument(); // Escape is ignored

    await user.click(within(reveal).getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith(SECRET);
    expect(await within(reveal).findByText("Copied")).toBeInTheDocument();

    await user.click(within(reveal).getByRole("button", { name: "I've saved it" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create key" })).toHaveFocus(); // back to the trigger
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument(); // gone for good
    expect(await screen.findByText("production")).toBeInTheDocument(); // list reloaded
  });

  it("validates the name and shows API errors inside the dialog", async () => {
    api.GET.mockReturnValue(apiResponse(200, { items: [] }));
    api.POST.mockReturnValue(apiErrorResponse(409, "Active API key limit (25) reached; revoke a key first", null, "conflict_error"));
    const user = userEvent.setup();
    render(<KeysPage />);
    await screen.findByText("No API keys yet");
    await user.click(screen.getByRole("button", { name: "Create key" }));
    const dialog = screen.getByRole("dialog", { name: "Create API key" });

    await user.click(within(dialog).getByRole("button", { name: "Create key" }));
    expect(within(dialog).getByText(/Give the key a name/)).toBeInTheDocument();
    expect(api.POST).not.toHaveBeenCalled();

    await user.type(within(dialog).getByLabelText("Name"), "one too many");
    await user.click(within(dialog).getByRole("button", { name: "Create key" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Active API key limit (25) reached");
  });

  it("revokes a key after confirmation", async () => {
    api.GET.mockReturnValueOnce(apiResponse(200, { items: [KEY] })).mockReturnValue(
      apiResponse(200, { items: [{ ...KEY, is_active: false }] }),
    );
    api.DELETE.mockReturnValue(apiResponse(204));
    const user = userEvent.setup();
    render(<KeysPage />);

    await user.click(await screen.findByRole("button", { name: "Revoke production" }));
    const dialog = screen.getByRole("dialog", { name: "Revoke API key?" });
    expect(dialog).toHaveTextContent("stops working immediately");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus(); // safe default

    await user.click(within(dialog).getByRole("button", { name: "Revoke key" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.DELETE).toHaveBeenCalledWith("/v1/dashboard/keys/{key_id}", { params: { path: { key_id: "k1" } } });
    expect(await screen.findByText("Revoked")).toBeInTheDocument();
  });

  it("renames a key", async () => {
    api.GET.mockReturnValueOnce(apiResponse(200, { items: [KEY] })).mockReturnValue(
      apiResponse(200, { items: [{ ...KEY, name: "staging" }] }),
    );
    api.PATCH.mockReturnValue(apiResponse(200, { ...KEY, name: "staging" }));
    const user = userEvent.setup();
    render(<KeysPage />);

    await user.click(await screen.findByRole("button", { name: "Rename production" }));
    const input = within(screen.getByRole("dialog", { name: "Rename API key" })).getByLabelText("Name");
    expect(input).toHaveValue("production");
    await user.clear(input);
    await user.type(input, "staging{Enter}");

    await waitFor(() =>
      expect(api.PATCH).toHaveBeenCalledWith("/v1/dashboard/keys/{key_id}", {
        params: { path: { key_id: "k1" } },
        body: { name: "staging" },
      }),
    );
    expect(await screen.findByText("staging")).toBeInTheDocument();
  });

  it("offers a retry when loading fails", async () => {
    api.GET.mockReturnValueOnce(apiResponse(502)).mockReturnValue(apiResponse(200, { items: [KEY] }));
    const user = userEvent.setup();
    render(<KeysPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load your keys");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("production")).toBeInTheDocument();
  });
});
