import type { ReactElement } from "react";
import { render as rtlRender, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi } from "vitest";
import { QuickCapture } from "./QuickCapture";

vi.mock("../api/client", () => ({ api: { createEvent: vi.fn(async (b) => ({ id: "e", ...b })), createTodo: vi.fn(), quickAdd: vi.fn(), run: vi.fn(), acceptProposal: vi.fn() } }));
import { api } from "../api/client";

const render = (ui: ReactElement) =>
  rtlRender(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);

describe("QuickCapture", () => {
  it("parses free text into proposals", async () => {
    const base = { id: "r9", kind: "quick_add", status: "ok", result_md: "Found one.", error: null, project_id: null, created_at: "2026-01-05T10:00:00Z" };
    const proposal = { kind: "todo", project: "clmi", title: "Email Bob", reason: "asked", event_kind: "", when: null, due: null, body_md: "", status: "suggested", applied_txn: null, applied_id: null };
    (api.quickAdd as any).mockResolvedValue({ ...base, status: "queued", proposals: [] });
    (api.run as any).mockResolvedValue({ run: { ...base, proposals: [proposal] }, txn_ids: [] });
    render(<QuickCapture projects={[{ id: "p1", slug: "clmi", title: "CLMI" } as any]} onCreated={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Describe it"), { target: { value: "email bob tomorrow" } });
    fireEvent.click(screen.getByRole("button", { name: "Parse" }));
    await waitFor(() => expect(api.quickAdd).toHaveBeenCalledWith({ text: "email bob tomorrow" }));
    expect(await screen.findByText("Email Bob")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("submits an event with an ISO UTC timestamp and intent", async () => {
    const onCreated = vi.fn();
    render(<QuickCapture projects={[{ id: "p1", slug: "clmi", title: "CLMI" } as any]} onCreated={onCreated} />);
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Freeze" } });
    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "decision" } });
    fireEvent.change(screen.getByLabelText("When"), { target: { value: "2026-01-05T10:00" } });
    fireEvent.change(screen.getByLabelText("Intent"), { target: { value: "after advisor sync" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const body = (api.createEvent as any).mock.calls[0][0];
    expect(body.project_id).toBe("p1");
    expect(body.t_start).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$/);
    expect(body.intent).toBe("after advisor sync");
  });

  it("sends no due for a todo with a blank date", async () => {
    const onCreated = vi.fn();
    (api.createTodo as any).mockResolvedValue({ id: "t" });
    render(<QuickCapture projects={[{ id: "p1", slug: "clmi", title: "CLMI" } as any]} onCreated={onCreated} />);
    fireEvent.change(screen.getByLabelText("Entry type"), { target: { value: "todo" } });
    expect((screen.getByLabelText("When") as HTMLInputElement).value).toBe("");
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Email Bob" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const body = (api.createTodo as any).mock.calls[0][0];
    expect(body.title).toBe("Email Bob");
    expect("due" in body).toBe(false);
  });

  it("shows a validation message for an invalid event time and does not submit", async () => {
    (api.createEvent as any).mockClear();
    const { container } = render(<QuickCapture projects={[{ id: "p1", slug: "clmi", title: "CLMI" } as any]} onCreated={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "X" } });
    fireEvent.change(screen.getByLabelText("When"), { target: { value: "" } });
    fireEvent.submit(container.querySelector("form")!);
    expect((await screen.findByRole("alert")).textContent).toBe("Enter a valid date and time");
    expect(api.createEvent).not.toHaveBeenCalled();
  });

  it("disables Save for an event when there are no projects", () => {
    render(<QuickCapture projects={[]} onCreated={vi.fn()} />);
    expect(screen.getByText("Create a project first")).toBeTruthy();
    expect((screen.getByText("Save") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Entry type"), { target: { value: "todo" } });
    expect(screen.queryByText("Create a project first")).toBeNull();
    expect((screen.getByText("Save") as HTMLButtonElement).disabled).toBe(false);
  });
});
