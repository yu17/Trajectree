import { expect } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import type { Catalog } from "../api/types";

// A catalog for tests: claude offers two models, codex is unavailable.
export const testCatalog: Catalog = {
  runners: [
    {
      runner: "claude",
      available: true,
      models: [
        { runner: "claude", model: "claude-fable-5-1", display: null, source: "builtin" },
        { runner: "claude", model: "claude-sonnet-5-5", display: "Sonnet 5.5", source: "api" },
      ],
      refreshed_at: "2026-10-24T09:00:00Z",
      error: null,
    },
    { runner: "codex", available: false, models: [], refreshed_at: null, error: "codex is not installed" },
  ],
};

export const FABLE = { runner: "claude", model: "claude-fable-5-1" };
export const FABLE_VALUE = "claude|claude-fable-5-1";

// Picks claude / claude-fable-5-1 in a model picker's select (the only one on screen unless given).
export async function pickFable(select?: HTMLElement) {
  const el = select ?? (await screen.findByRole("combobox", { name: "Model" }));
  await waitFor(() => expect(el.querySelector(`option[value="${FABLE_VALUE}"]`)).not.toBeNull());
  fireEvent.change(el, { target: { value: FABLE_VALUE } });
}
