import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const router = { replace: vi.fn(), refresh: vi.fn() };
let searchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  useSearchParams: () => searchParams,
}));

const post = vi.fn();
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...original, api: { POST: (...args: unknown[]) => post(...args) } };
});

import { AuthForm } from "./auth-form";

function apiResult(status: number, body: unknown) {
  const response = new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  return Promise.resolve(response.ok ? { data: body, response } : { error: body, response });
}

function apiError(status: number, message: string, param: string | null = null) {
  return apiResult(status, { error: { type: "x", message, param }, request_id: "r" });
}

beforeEach(() => {
  post.mockReset();
  router.replace.mockReset();
  router.refresh.mockReset();
  searchParams = new URLSearchParams();
});

async function fill(email: string, password: string) {
  const user = userEvent.setup();
  if (email) await user.type(screen.getByLabelText("Email"), email);
  if (password) await user.type(screen.getByLabelText("Password"), password);
  return user;
}

describe("AuthForm", () => {
  it("validates before calling the API", async () => {
    render(<AuthForm mode="login" />);
    const user = await fill("not-an-email", "");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
    expect(screen.getByText("Enter your password.")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
    expect(post).not.toHaveBeenCalled();
  });

  it("enforces the password length on sign-up only", async () => {
    render(<AuthForm mode="signup" />);
    const user = await fill("ada@example.com", "short");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(screen.getByText("Use at least 10 characters.")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("signs in and goes to the requested page", async () => {
    searchParams = new URLSearchParams("next=/settings");
    post.mockReturnValue(apiResult(200, { user: {}, csrf_token: "t" }));
    render(<AuthForm mode="login" />);
    const user = await fill("  ada@example.com ", "correct horse battery staple");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/settings"));
    expect(post).toHaveBeenCalledWith("/v1/auth/login", {
      body: { email: "ada@example.com", password: "correct horse battery staple" },
    });
  });

  it("ignores unsafe ?next= targets", async () => {
    searchParams = new URLSearchParams("next=https://evil.example.com");
    post.mockReturnValue(apiResult(201, { user: {}, csrf_token: "t" }));
    render(<AuthForm mode="signup" />);
    const user = await fill("ada@example.com", "correct horse battery staple");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/overview"));
    expect(post.mock.calls[0][0]).toBe("/v1/auth/signup");
  });

  it("shows the API's message for failed sign-in", async () => {
    post.mockReturnValue(apiError(401, "Invalid email or password"));
    render(<AuthForm mode="login" />);
    const user = await fill("ada@example.com", "wrong password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password");
    expect(router.replace).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("attaches field errors from the API to the right input", async () => {
    post.mockReturnValue(apiError(409, "An account with this email already exists", "email"));
    render(<AuthForm mode="signup" />);
    const user = await fill("ada@example.com", "correct horse battery staple");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByText("An account with this email already exists")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
  });

  it("disables the button while submitting", async () => {
    let finish: (value: unknown) => void = () => undefined;
    post.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    render(<AuthForm mode="login" />);
    const user = await fill("ada@example.com", "correct horse battery staple");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
    finish(await apiResult(200, {}));
    await waitFor(() => expect(router.replace).toHaveBeenCalled());
  });

  it("links between sign-in and sign-up", () => {
    const { unmount } = render(<AuthForm mode="login" />);
    expect(screen.getByRole("link", { name: "Create an account" })).toHaveAttribute("href", "/signup");
    unmount();
    render(<AuthForm mode="signup" />);
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
  });
});
