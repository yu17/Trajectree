import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SettingsView } from "./SettingsView";
import type { ConnectorStatus, TokenCreate, TokenOut } from "../api/types";

const mocks = vi.hoisted(() => ({ tokens: vi.fn(), createToken: vi.fn(), revokeToken: vi.fn(), connectors: vi.fn(), mounts: vi.fn(), runnerStatus: vi.fn(), scheduler: vi.fn() }));
vi.mock("../api/client", async importOriginal => {
  const real = await importOriginal<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, ...mocks } };
});

const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);
const token = (id: string, over: Partial<TokenOut> = {}): TokenOut => ({ id, label: id, scope: {}, actor_kind: "session", expires_at: null, created_at: "2026-10-01T10:00:00Z", ...over });
const conn = (name: ConnectorStatus["name"], over: Partial<ConnectorStatus> = {}): ConnectorStatus => ({ name, configured: false, last_run: null, errors: [], detail: null, ...over });

let store: TokenOut[];

describe("SettingsView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store = [];
    mocks.tokens.mockImplementation(async () => store.map(t => ({ ...t })));
    mocks.createToken.mockImplementation(async (body: TokenCreate) => {
      const t = token("t1", { label: body.label, scope: body.scope, actor_kind: body.actor_kind ?? "session" });
      store = [t, ...store];
      return { ...t, secret: "trj_secretvalue" };
    });
    mocks.revokeToken.mockImplementation(async (id: string) => { store = store.filter(t => t.id !== id); });
    mocks.connectors.mockResolvedValue([]);
    mocks.mounts.mockResolvedValue({ claude_home: "/claude", backends_root: null, path_map: [["/home/u", "/mnt/u"]], data_dir: "/data", cfg_dir: "/data/cfg", exports_dir: "/data/exports" });
    mocks.runnerStatus.mockResolvedValue({ claude: { available: true, version: "2.1.309", authenticated: true }, codex: { available: false, version: null, authenticated: false }, backends: [] });
    mocks.scheduler.mockResolvedValue({ last_tick: null, interval_s: 30, next_fires: [{ routine: "weekly-review", at: "2026-10-26T08:00:00Z" }] });
  });

  it("lists tokens, creates a chatmaid token showing the secret once, and revokes", async () => {
    const user = userEvent.setup();
    store = [token("old", { label: "old", scope: { write: false } }), token("g", { label: "group", scope: { projects: ["a", "b"], write: true }, actor_kind: "agent" })];
    wrap(<SettingsView />);
    expect(await screen.findByText("write: projects a,b")).toBeTruthy();
    expect(within(screen.getByText("old").closest("tr")!).getByText("read-only")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Tokens" })).toBeTruthy();

    await user.type(screen.getByLabelText("Label"), "chatmaid");
    await user.selectOptions(screen.getByLabelText("Scope"), "chatmaid");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(mocks.createToken).toHaveBeenCalledWith({ label: "chatmaid", scope: { projects: [], tools: ["chatmaid"], write: false }, actor_kind: "connector" }));
    const secret = await screen.findByText("trj_secretvalue");
    expect(secret.tagName).toBe("CODE");
    expect(screen.getByText("Shown once; store it now")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy();
    expect(await screen.findByText("tools: chatmaid")).toBeTruthy();

    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const row = screen.getByText("old").closest("tr")!;
    await user.click(within(row).getByRole("button", { name: "Revoke" }));
    expect(mocks.revokeToken).not.toHaveBeenCalled();
    await user.click(within(row).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(mocks.revokeToken).toHaveBeenCalledWith("old"));
    await waitFor(() => expect(screen.queryByText("old")).toBeNull());
    confirm.mockRestore();
  });

  it("shows No tokens when there are none", async () => {
    wrap(<SettingsView />);
    expect(await screen.findByText("No tokens")).toBeTruthy();
  });

  it("shows connector configuration", async () => {
    mocks.connectors.mockResolvedValue([
      conn("local-git", { configured: true, last_run: "2026-10-22T10:00:00Z", errors: ["clone failed", "second"] }),
      conn("github", { configured: true, detail: "token stored" }),
      conn("slurm"),
    ]);
    wrap(<SettingsView />);
    const git = (await screen.findByText("local-git")).closest("tr")!;
    expect(within(git).getByText("configured")).toBeTruthy();
    expect(within(git).getByText("clone failed").className).toContain("form-error");
    expect(within(git).queryByText("second")).toBeNull();
    expect(within(git).queryByText("never")).toBeNull();
    const slurm = screen.getByText("slurm").closest("tr")!;
    expect(within(slurm).getByText("not configured")).toBeTruthy();
    expect(within(slurm).getByText("never")).toBeTruthy();
    expect(screen.getByText("token stored")).toBeTruthy();
    expect(screen.queryByText("No connectors configured")).toBeNull();
  });

  it("says when no connector is configured", async () => {
    mocks.connectors.mockResolvedValue([conn("slurm")]);
    wrap(<SettingsView />);
    expect(await screen.findByText("No connectors configured")).toBeTruthy();
  });

  it("shows the runner, mounts and scheduler", async () => {
    wrap(<SettingsView />);
    expect(await screen.findByText(/claude 2\.1\.309 · logged in/)).toBeTruthy();
    expect(await screen.findByText("/data/cfg")).toBeTruthy();
    expect(screen.getByText("/home/u -> /mnt/u")).toBeTruthy();
    expect(screen.getByText(/last tick never · every 30 s/)).toBeTruthy();
    expect(await screen.findByText(/weekly-review/)).toBeTruthy();
    for (const name of ["Runner", "Mounts", "Scheduler", "Connectors"]) expect(screen.getByRole("heading", { name })).toBeTruthy();
  });
});
