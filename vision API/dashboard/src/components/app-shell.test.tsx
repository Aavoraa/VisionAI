import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LayoutDashboard, Shield } from "lucide-react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const router = { replace: vi.fn(), refresh: vi.fn() };
let pathname = "/overview";
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  usePathname: () => pathname,
}));

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...original, api: { GET: (...a: unknown[]) => get(...a), POST: (...a: unknown[]) => post(...a) } };
});

import { SessionProvider, loginUrl } from "@/lib/session";

import { AppShell } from "./app-shell";
import { isActive, navItemsFor, type NavItem } from "./nav";
import { UsageBar } from "./ui";

const USER = {
  id: "u1",
  email: "ada@example.com",
  role: "user" as const,
  is_active: true,
  created_at: "2026-09-01T00:00:00Z",
  last_login_at: null,
};

function respond(status: number, body: unknown) {
  const response = new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
  return Promise.resolve(response.ok ? { data: body ?? undefined, response } : { error: body, response });
}

function renderShell() {
  return render(
    <SessionProvider>
      <AppShell>
        <p>page content</p>
      </AppShell>
    </SessionProvider>,
  );
}

beforeEach(() => {
  get.mockReset();
  post.mockReset();
  router.replace.mockReset();
  pathname = "/overview";
});

describe("navigation model", () => {
  const items: NavItem[] = [
    { href: "/overview", label: "Overview", icon: LayoutDashboard },
    { href: "/admin/users", label: "Users", icon: Shield, adminOnly: true },
  ];

  it("hides admin-only entries from regular users", () => {
    expect(navItemsFor({ role: "user" }, items).map((i) => i.label)).toEqual(["Overview"]);
    expect(navItemsFor({ role: "admin" }, items).map((i) => i.label)).toEqual(["Overview", "Users"]);
  });

  it("matches active routes by segment", () => {
    expect(isActive("/admin/users/1", "/admin/users")).toBe(true);
    expect(isActive("/overview", "/overview")).toBe(true);
    expect(isActive("/overviewx", "/overview")).toBe(false);
  });

  it("builds sign-in URLs that return to the current page", () => {
    expect(loginUrl("/settings")).toBe("/login?next=%2Fsettings");
    expect(loginUrl("/")).toBe("/login");
  });
});

describe("AppShell with session", () => {
  it("shows a loading state, then the user and the page", async () => {
    get.mockReturnValue(respond(200, USER));
    renderShell();
    expect(screen.getByLabelText("Loading")).toBeInTheDocument();

    expect(await screen.findByText("page content")).toBeInTheDocument();
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
    const nav = screen.getAllByRole("navigation", { name: "Main" })[0];
    expect(within(nav).getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Settings" })).not.toHaveAttribute("aria-current");
  });

  it.each([401, 403])("sends the user to sign-in on %i", async (status) => {
    pathname = "/settings";
    get.mockReturnValue(respond(status, { error: { type: "authentication_error", message: "Not signed in", param: null } }));
    renderShell();
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login?next=%2Fsettings"));
    expect(screen.queryByText("page content")).not.toBeInTheDocument();
  });

  it("offers a retry when the API is unreachable", async () => {
    get.mockReturnValueOnce(respond(502, null)).mockReturnValueOnce(respond(200, USER));
    renderShell();
    expect(await screen.findByRole("alert")).toHaveTextContent("The service is unavailable");

    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("page content")).toBeInTheDocument();
  });

  it("signs out through the API and returns to sign-in", async () => {
    get.mockReturnValue(respond(200, USER));
    post.mockReturnValue(respond(204, null));
    renderShell();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Sign out" }));
    expect(post).toHaveBeenCalledWith("/v1/auth/logout");
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login"));
  });

  it("shows the Administration section only to administrators", async () => {
    get.mockReturnValue(respond(200, USER));
    const { unmount } = renderShell();
    await screen.findByText("page content");
    expect(screen.queryByRole("heading", { name: "Administration" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
    unmount();

    get.mockReturnValue(respond(200, { ...USER, role: "admin" }));
    renderShell();
    await screen.findByText("page content");
    expect(screen.getAllByRole("heading", { name: "Administration" })[0]).toBeInTheDocument();
    for (const name of ["Users", "Plans", "Platform usage"]) {
      expect(screen.getAllByRole("link", { name })[0]).toBeInTheDocument();
    }
  });

  it("opens and closes the mobile menu", async () => {
    get.mockReturnValue(respond(200, USER));
    renderShell();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Open menu" }));
    expect(screen.getByRole("dialog", { name: "Menu" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Menu" })).not.toBeInTheDocument();
  });
});

describe("UsageBar", () => {
  it("exposes progress accessibly and states limits in text, not colour alone", () => {
    const { rerender } = render(<UsageBar label="Requests" ratio={0.25} valueText="1,250 of 5,000" />);
    const bar = screen.getByRole("progressbar", { name: "Requests" });
    expect(bar).toHaveAttribute("aria-valuenow", "25");
    expect(bar).toHaveAttribute("aria-valuetext", "1,250 of 5,000");

    rerender(<UsageBar label="Requests" ratio={0.85} valueText="4,250 of 5,000" />);
    expect(screen.getByText(/nearly used/)).toBeInTheDocument();
    rerender(<UsageBar label="Requests" ratio={1} valueText="5,000 of 5,000" />);
    expect(screen.getByText(/limit reached/)).toBeInTheDocument();
  });
});
