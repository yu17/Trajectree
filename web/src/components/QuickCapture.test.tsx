import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { QuickCapture } from "./QuickCapture";

vi.mock("../api/client", () => ({ api: { createEvent: vi.fn(async (b) => ({ id: "e", ...b })), createTodo: vi.fn() } }));
import { api } from "../api/client";

describe("QuickCapture", () => {
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
});
