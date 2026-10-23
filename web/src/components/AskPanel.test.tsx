import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi } from "vitest";
import { AskPanel } from "./AskPanel";

const api = vi.hoisted(() => ({ context: vi.fn(), ask: vi.fn(), run: vi.fn(), acceptProposal: vi.fn() }));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));
const projects = [{ id: "p1", slug: "clmi", title: "CLMI", kind: "research", status: "active" }, { id: "p2", slug: "ccx", title: "ccx", kind: "tooling", status: "active" }] as any;
const run = (status: string) => ({ run: { id: "r1", kind: "ask", status, result_md: status === "ok" ? "Blocked on data." : "", proposals: [], error: null, project_id: null, runner: "claude", backend: "claude", model: null, session_id: null, started: null, finished: null, tokens_in: 0, tokens_out: 0, tokens_cache: 0, est_usd: 0, turns: 0, duration_ms: 0, log_path: null, token_id: null, props: {}, created_at: "2026-01-05T10:00:00Z" }, txn_ids: [] });

describe("AskPanel", () => {
  it("shows the pack size, asks with the selected slugs and renders the answer", async () => {
    api.context.mockResolvedValue({ markdown: "", tokens_est: 1234, dropped: [], projects: ["clmi", "ccx"] });
    api.ask.mockResolvedValue(run("queued").run);
    api.run.mockResolvedValueOnce(run("running")).mockResolvedValue(run("ok"));
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AskPanel projects={projects} /></QueryClientProvider>);
    expect(await screen.findByText(/pack ≈ 1234 tokens/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText("ccx"));
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "What is blocked?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(api.ask).toHaveBeenCalledWith(expect.objectContaining({ question: "What is blocked?", projects: ["clmi"] })));
    expect(await screen.findByText("Blocked on data.", {}, { timeout: 4000 })).toBeTruthy();
  });

  it("offers only active projects and disables Ask when none is checked", async () => {
    api.context.mockResolvedValue({ markdown: "", tokens_est: 10, dropped: [], projects: [] });
    api.ask.mockClear();
    const withPaused = [...projects, { id: "p3", slug: "old", title: "Old", kind: "research", status: "paused" }] as any;
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AskPanel projects={withPaused} /></QueryClientProvider>);
    expect(screen.queryByLabelText("Old")).toBeNull();
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Q?" } });
    expect((screen.getByRole("button", { name: "Ask" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByLabelText("CLMI"));
    fireEvent.click(screen.getByLabelText("ccx"));
    expect((screen.getByRole("button", { name: "Ask" }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.ask).not.toHaveBeenCalled();
  });

  it("excludes paused projects from the ask call", async () => {
    api.context.mockResolvedValue({ markdown: "", tokens_est: 10, dropped: [], projects: [] });
    api.ask.mockResolvedValue(run("queued").run);
    api.run.mockResolvedValue(run("ok"));
    const withPaused = [...projects, { id: "p3", slug: "old", title: "Old", kind: "research", status: "paused" }] as any;
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AskPanel projects={withPaused} /></QueryClientProvider>);
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Q?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(api.ask).toHaveBeenCalledWith(expect.objectContaining({ projects: ["clmi", "ccx"] })));
  });
});
