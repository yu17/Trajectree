import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { MarpPreview } from "./MarpPreview";

describe("MarpPreview", () => {
  it("renders two slides into the iframe document", async () => {
    render(<MarpPreview source={"# One\n\n---\n\n# Two"} />);
    const frame = await screen.findByTitle("Preview");
    await waitFor(() => expect(frame.getAttribute("data-slides")).toBe("2"), { timeout: 2000 });
    expect(frame.getAttribute("srcdoc")).toContain("One");
    expect(frame.getAttribute("sandbox")).toBe("");
    const doc = new DOMParser().parseFromString(frame.getAttribute("srcdoc") ?? "", "text/html");
    const csp = doc.head.querySelector('meta[http-equiv="Content-Security-Policy"]');
    expect(csp?.getAttribute("content")).toBe("default-src 'none'; style-src 'unsafe-inline'; img-src data:");
    expect(doc.querySelector("script")).toBeNull(); // marp's browser script is off
  });

  it("does not render raw HTML from the source", async () => {
    render(<MarpPreview source={"# Hi\n\n<script>alert(1)</script>"} />);
    const frame = await screen.findByTitle("Preview");
    await waitFor(() => expect(frame.getAttribute("data-slides")).toBe("1"), { timeout: 2000 });
    expect(frame.getAttribute("srcdoc")).not.toContain("<script>alert(1)</script>");
  });
});
