import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ GET: vi.fn(), POST: vi.fn(), PATCH: vi.fn(), DELETE: vi.fn() }));
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...original, api };
});
const session = vi.hoisted(() => ({ user: { id: "admin-1", role: "admin" as "admin" | "user" } }));
vi.mock("@/lib/session", () => ({ useCurrentUser: () => session.user }));

import { USAGE_REPORT, apiErrorResponse, apiResponse } from "@/test-utils";

import AdminLayout from "./layout";
import PlansPage from "./plans/page";
import PlatformUsagePage from "./usage/page";
import UsersPage from "./users/page";

const ADA = {
  id: "u-ada",
  email: "ada@example.com",
  role: "user",
  is_active: true,
  has_password: true,
  created_at: "2026-09-01T00:00:00Z",
  last_login_at: "2026-09-24T09:00:00Z",
  plan_id: null,
  plan_name: "Default",
  active_keys: 2,
  requests_30d: 604,
};
const ME = { ...ADA, id: "admin-1", email: "root@example.com", role: "admin", requests_30d: 0 };
const DEFAULT_PLAN = {
  id: "p-default",
  name: "Default",
  is_default: true,
  price_per_1k_input_tokens_usd: "0.000500",
  price_per_1k_output_tokens_usd: "0.001500",
  price_per_image_usd: "0.002000",
  monthly_request_limit: 5000,
  monthly_spend_limit_usd: null,
  users: 2,
};
const PRO_PLAN = { ...DEFAULT_PLAN, id: "p-pro", name: "Pro", is_default: false, monthly_request_limit: null, users: 0 };
const KEY = {
  id: "k1",
  name: "production",
  prefix: "sk_live_AbCd",
  is_active: true,
  expires_at: null,
  last_used_at: null,
  created_at: "2026-09-01T00:00:00Z",
};

function page(items: unknown[], total = items.length) {
  return apiResponse(200, { items, total, limit: 25, offset: 0 });
}

function routeGet(routes: Record<string, () => unknown>) {
  api.GET.mockImplementation((path: string) => {
    const handler = routes[path];
    if (!handler) throw new Error(`unexpected GET ${path}`);
    return handler();
  });
}

beforeEach(() => {
  Object.values(api).forEach((fn) => fn.mockReset());
  session.user = { id: "admin-1", role: "admin" };
});

describe("admin guard", () => {
  it("shows a clear message to regular users instead of admin pages", () => {
    session.user = { id: "u-ada", role: "user" };
    render(
      <AdminLayout params={Promise.resolve({})}>
        <p>secret admin content</p>
      </AdminLayout>,
    );
    expect(screen.getByRole("heading", { name: "Administrator access required" })).toBeInTheDocument();
    expect(screen.queryByText("secret admin content")).not.toBeInTheDocument();
  });
});

describe("Users page", () => {
  it("lists users with plan, activity and status", async () => {
    routeGet({ "/v1/admin/users": () => page([ADA, { ...ME }]), "/v1/admin/plans": () => apiResponse(200, { items: [DEFAULT_PLAN] }) });
    render(<UsersPage />);
    const table = await screen.findByRole("table", { name: "Users" });
    const [ada, me] = within(table).getAllByRole("row").slice(1);
    expect(within(ada).getByText("ada@example.com")).toBeInTheDocument();
    expect(within(ada).getByText("Active")).toBeInTheDocument();
    expect(within(ada).getByText("604")).toBeInTheDocument();
    expect(within(me).getByText("Admin")).toBeInTheDocument();
    expect(screen.getByText("1–2 of 2")).toBeInTheDocument();
  });

  it("searches with debounce and resets to the first page", async () => {
    routeGet({ "/v1/admin/users": () => page([ADA], 60), "/v1/admin/plans": () => apiResponse(200, { items: [] }) });
    const user = userEvent.setup();
    render(<UsersPage />);
    await screen.findByRole("table", { name: "Users" });

    await user.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() =>
      expect(api.GET).toHaveBeenLastCalledWith("/v1/admin/users", { params: { query: { search: undefined, limit: 25, offset: 25 } } }),
    );

    await user.type(screen.getByLabelText("Search by email"), "ada");
    await waitFor(() =>
      expect(api.GET).toHaveBeenLastCalledWith("/v1/admin/users", { params: { query: { search: "ada", limit: 25, offset: 0 } } }),
    );
    const searchCalls = api.GET.mock.calls.filter(([path, opts]) => path === "/v1/admin/users" && opts.params.query.search);
    expect(searchCalls).toHaveLength(1); // one request for "ada", not one per keystroke
  });

  it("deactivates a user and revokes their key from the manage dialog", async () => {
    routeGet({
      "/v1/admin/users": () => page([ADA]),
      "/v1/admin/plans": () => apiResponse(200, { items: [DEFAULT_PLAN, PRO_PLAN] }),
      "/v1/admin/users/{user_id}/keys": () => apiResponse(200, { user_id: ADA.id, items: [KEY] }),
    });
    api.PATCH.mockReturnValue(apiResponse(200, { ...ADA, is_active: false }));
    api.DELETE.mockReturnValue(apiResponse(204));
    const user = userEvent.setup();
    render(<UsersPage />);

    await user.click(await screen.findByRole("button", { name: "Manage ada@example.com" }));
    const dialog = screen.getByRole("dialog", { name: "Manage user" });
    await user.click(within(dialog).getByRole("button", { name: "Deactivate account" }));
    await waitFor(() =>
      expect(api.PATCH).toHaveBeenCalledWith("/v1/admin/users/{user_id}", {
        params: { path: { user_id: "u-ada" } },
        body: { is_active: false },
      }),
    );
    expect(await within(dialog).findByRole("button", { name: "Reactivate account" })).toBeInTheDocument();

    await user.click(await within(dialog).findByRole("button", { name: "Revoke production" }));
    await user.click(within(dialog).getByRole("button", { name: "Confirm revoke" }));
    await waitFor(() =>
      expect(api.DELETE).toHaveBeenCalledWith("/v1/admin/keys/{key_id}", { params: { path: { key_id: "k1" } } }),
    );
  });

  it("changes a user's plan (empty choice = default)", async () => {
    routeGet({
      "/v1/admin/users": () => page([ADA]),
      "/v1/admin/plans": () => apiResponse(200, { items: [DEFAULT_PLAN, PRO_PLAN] }),
      "/v1/admin/users/{user_id}/keys": () => apiResponse(200, { user_id: ADA.id, items: [] }),
    });
    api.PATCH.mockReturnValue(apiResponse(200, { ...ADA, plan_id: "p-pro", plan_name: "Pro" }));
    const user = userEvent.setup();
    render(<UsersPage />);

    await user.click(await screen.findByRole("button", { name: "Manage ada@example.com" }));
    const dialog = screen.getByRole("dialog", { name: "Manage user" });
    const save = within(dialog).getByRole("button", { name: "Save plan" });
    expect(save).toBeDisabled(); // nothing changed yet
    expect(within(dialog).getByRole("option", { name: "Follow default (currently Default)" })).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Plan"), "p-pro");
    await user.click(save);
    await waitFor(() =>
      expect(api.PATCH).toHaveBeenCalledWith("/v1/admin/users/{user_id}", {
        params: { path: { user_id: "u-ada" } },
        body: { plan_id: "p-pro" },
      }),
    );
  });

  it("does not let admins lock themselves out", async () => {
    routeGet({
      "/v1/admin/users": () => page([ME]),
      "/v1/admin/plans": () => apiResponse(200, { items: [DEFAULT_PLAN] }),
      "/v1/admin/users/{user_id}/keys": () => apiResponse(200, { user_id: ME.id, items: [] }),
    });
    const user = userEvent.setup();
    render(<UsersPage />);
    await user.click(await screen.findByRole("button", { name: "Manage root@example.com" }));
    const dialog = screen.getByRole("dialog", { name: "Manage user" });
    expect(within(dialog).getByRole("button", { name: "Deactivate account" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Remove administrator" })).toBeDisabled();
    expect(within(dialog).getByText("You cannot deactivate or demote your own account.")).toBeInTheDocument();
  });
});

describe("Plans page", () => {
  it("lists plans with exact prices", async () => {
    routeGet({ "/v1/admin/plans": () => apiResponse(200, { items: [DEFAULT_PLAN, PRO_PLAN] }) });
    render(<PlansPage />);
    const table = await screen.findByRole("table", { name: "Plans" });
    const [first, second] = within(table).getAllByRole("row").slice(1);
    expect(within(first).getAllByText("Default")).toHaveLength(2); // the plan name and its Default badge
    expect(within(second).queryAllByText("Default")).toHaveLength(0);
    expect(within(first).getByText("$0.0005")).toBeInTheDocument();
    expect(within(first).getByText("5,000")).toBeInTheDocument();
    expect(within(second).getByText("Unlimited")).toBeInTheDocument();
  });

  it("creates a plan with exact decimal strings", async () => {
    routeGet({ "/v1/admin/plans": () => apiResponse(200, { items: [DEFAULT_PLAN] }) });
    api.POST.mockReturnValue(apiResponse(201, PRO_PLAN));
    const user = userEvent.setup();
    render(<PlansPage />);
    await screen.findByRole("table", { name: "Plans" });

    await user.click(screen.getByRole("button", { name: "Create plan" }));
    const dialog = screen.getByRole("dialog", { name: "Create plan" });
    await user.type(within(dialog).getByLabelText("Name"), "Pro");
    await user.type(within(dialog).getByLabelText("Per 1K input tokens"), "0.0004");
    await user.type(within(dialog).getByLabelText("Per 1K output tokens"), "0.0012");
    await user.type(within(dialog).getByLabelText("Per image"), "0.0015");
    await user.type(within(dialog).getByLabelText("Monthly request limit"), "100000");
    await user.click(within(dialog).getByRole("button", { name: "Create plan" }));

    await waitFor(() =>
      expect(api.POST).toHaveBeenCalledWith("/v1/admin/plans", {
        body: {
          name: "Pro",
          price_per_1k_input_tokens_usd: "0.0004",
          price_per_1k_output_tokens_usd: "0.0012",
          price_per_image_usd: "0.0015",
          monthly_request_limit: 100000,
          monthly_spend_limit_usd: null,
          is_default: false,
        },
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("edits only changed fields and cannot unset the default", async () => {
    routeGet({ "/v1/admin/plans": () => apiResponse(200, { items: [DEFAULT_PLAN] }) });
    api.PATCH.mockReturnValue(apiResponse(200, DEFAULT_PLAN));
    const user = userEvent.setup();
    render(<PlansPage />);

    await user.click(await screen.findByRole("button", { name: "Edit Default" }));
    const dialog = screen.getByRole("dialog", { name: "Edit Default" });
    expect(within(dialog).getByRole("checkbox", { name: /Default plan/ })).toBeDisabled();
    const image = within(dialog).getByLabelText("Per image");
    expect(image).toHaveValue("0.002");
    await user.clear(image);
    await user.type(image, "0.0025");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));

    await waitFor(() =>
      expect(api.PATCH).toHaveBeenCalledWith("/v1/admin/plans/{plan_id}", {
        params: { path: { plan_id: "p-default" } },
        body: { price_per_image_usd: "0.0025" },
      }),
    );
  });

  it("shows validation and API errors on the right field", async () => {
    routeGet({ "/v1/admin/plans": () => apiResponse(200, { items: [DEFAULT_PLAN] }) });
    api.POST.mockReturnValue(apiErrorResponse(409, "A plan with this name already exists", null, "conflict_error"));
    const user = userEvent.setup();
    render(<PlansPage />);
    await screen.findByRole("table", { name: "Plans" });
    await user.click(screen.getByRole("button", { name: "Create plan" }));
    const dialog = screen.getByRole("dialog", { name: "Create plan" });

    await user.type(within(dialog).getByLabelText("Per image"), "0.0000001");
    await user.click(within(dialog).getByRole("button", { name: "Create plan" }));
    expect(within(dialog).getByText("Give the plan a name.")).toBeInTheDocument();
    expect(within(dialog).getByText(/at most 6 decimals/)).toBeInTheDocument();
    expect(api.POST).not.toHaveBeenCalled();

    await user.type(within(dialog).getByLabelText("Name"), "default");
    await user.clear(within(dialog).getByLabelText("Per image"));
    for (const label of ["Per 1K input tokens", "Per 1K output tokens", "Per image"]) {
      await user.type(within(dialog).getByLabelText(label), "0");
    }
    await user.click(within(dialog).getByRole("button", { name: "Create plan" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("A plan with this name already exists");
  });
});

describe("Platform usage page", () => {
  it("summarises all users", async () => {
    routeGet({
      "/v1/admin/usage": () =>
        apiResponse(200, {
          ...USAGE_REPORT,
          active_users: 17,
          top_users: [{ user_id: "u-ada", email: "ada@example.com", requests: 6, input_tokens: 100, output_tokens: 20 }],
        }),
    });
    render(<PlatformUsagePage />);
    expect(await screen.findByRole("group", { name: "Active users" })).toHaveTextContent("17");
    expect(screen.getByRole("group", { name: "Charges" })).toHaveTextContent("$0.02");
    const top = screen.getByRole("table", { name: "Top users by requests" });
    expect(within(top).getByText("ada@example.com")).toBeInTheDocument();
    expect(within(top).getByText("120")).toBeInTheDocument();
    await act(async () => undefined);
  });
});
