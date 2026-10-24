import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ModelPicker } from "./ModelPicker";
import { FABLE, FABLE_VALUE, testCatalog } from "./testCatalog";

const api = vi.hoisted(() => ({ models: vi.fn() }));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));

const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);

describe("ModelPicker", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.models.mockResolvedValue(testCatalog);
  });

  it("groups models under their runner and starts at the placeholder", async () => {
    wrap(<ModelPicker value={null} onChange={() => {}} />);
    const select = (await screen.findByRole("combobox", { name: "Model" })) as HTMLSelectElement;
    await screen.findByRole("option", { name: "Sonnet 5.5" });
    expect(select.value).toBe("");
    const placeholder = screen.getByRole("option", { name: "Choose a model…" }) as HTMLOptionElement;
    expect(placeholder.disabled).toBe(true);
    const claude = select.querySelector('optgroup[label="claude"]')!;
    expect(Array.from(claude.querySelectorAll("option"), o => o.textContent)).toEqual(["default", "claude-fable-5-1", "Sonnet 5.5"]);
    const codex = select.querySelector('optgroup[label="codex (unavailable: codex is not installed)"]') as HTMLOptGroupElement;
    expect(codex.disabled).toBe(true);
  });

  it("reports the runner and the model on change", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    wrap(<ModelPicker value={null} onChange={onChange} />);
    await screen.findByRole("option", { name: "Sonnet 5.5" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Model" }), FABLE_VALUE);
    expect(onChange).toHaveBeenCalledWith(FABLE);
  });

  it("reports a runner alone as a null model", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    wrap(<ModelPicker value={null} onChange={onChange} />);
    await screen.findByRole("option", { name: "Sonnet 5.5" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Model" }), "claude|");
    expect(onChange).toHaveBeenCalledWith({ runner: "claude", model: null });
  });

  it("keeps a held runner-only choice while the runner is available, and clears it when not", async () => {
    const onChange = vi.fn();
    wrap(<ModelPicker value={{ runner: "claude", model: null }} onChange={onChange} />);
    await screen.findByRole("option", { name: "Sonnet 5.5" });
    expect((screen.getByRole("combobox", { name: "Model" }) as HTMLSelectElement).value).toBe("claude|");
    expect(onChange).not.toHaveBeenCalled();
    wrap(<ModelPicker value={{ runner: "codex", model: null }} onChange={onChange} />);
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(null));
  });

  it("shows the given value and a custom label", async () => {
    wrap(<ModelPicker value={FABLE} onChange={() => {}} label="Run on" />);
    await screen.findByRole("option", { name: "Sonnet 5.5" });
    expect((screen.getByRole("combobox", { name: "Run on" }) as HTMLSelectElement).value).toBe(FABLE_VALUE);
  });
});
