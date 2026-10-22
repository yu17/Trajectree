import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";

vi.mock("./api/client", async importOriginal => {
  const real = await importOriginal<typeof import("./api/client")>();
  const unauthorized = () => Promise.reject(new real.ApiError(401, "Unauthorized", { detail: "Unauthorized" }));
  return { ...real, api: { ...real.api, projects: vi.fn(unauthorized), timeline: vi.fn(unauthorized) } };
});

describe("App", () => {
  it("shows a login banner when the API answers 401", async () => {
    render(<QueryClientProvider client={new QueryClient()}><App /></QueryClientProvider>);
    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("Not logged in. Open the login URL printed by the daemon");
    expect(banner.textContent).toContain("docker compose logs trajectree | grep ready");
  });
});
