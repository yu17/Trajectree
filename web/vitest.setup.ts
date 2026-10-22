import { afterEach, expect } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => cleanup());

// A minimal stand-in for jest-dom's matcher (not a dependency): passes for a disabled form control.
expect.extend({
  toBeDisabled(received: unknown) {
    const pass = received instanceof HTMLElement && received.matches(":disabled");
    return { pass, message: () => `expected element ${pass ? "not " : ""}to be disabled` };
  },
});

declare module "vitest" {
  interface Matchers<R extends void | Promise<void> = void | Promise<void>, T = unknown> {
    toBeDisabled(): R;
  }
}
