import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SettingsView } from "./SettingsView";
import { testCatalog } from "../components/testCatalog";
import type { ConnectorStatus, TokenCreate, TokenOut } from "../api/types";

const mocks = vi.hoisted(() => ({ tokens: vi.fn(), createToken: vi.fn(), revokeToken: vi.fn(), connectors: vi.fn(), mounts: vi.fn(), runnerStatus: vi.fn(), scheduler: vi.fn(), models: vi.fn(), refreshModels: vi.fn(), editModels: vi.fn() }));
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
    mocks.models.mockResolvedValue(testCatalog);
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

  it("lists the model catalog and refreshes it", async () => {
    const user = userEvent.setup();
    const refreshed = {
      runners: [
        { ...testCatalog.runners[0], models: [{ runner: "claude", model: "claude-opus-5-5", display: null, source: "api" as const }] },
        testCatalog.runners[1],
      ],
    };
    mocks.refreshModels.mockResolvedValue(refreshed);
    wrap(<SettingsView />);
    expect(await screen.findByRole("heading", { name: "Models" })).toBeTruthy();
    const claude = await screen.findByRole("list", { name: "claude models" });
    expect(within(claude).getByText("claude-fable-5-1")).toBeTruthy();
    expect(within(claude).getByText("Sonnet 5.5")).toBeTruthy();
    expect(screen.getByText(`available · refreshed ${new Date("2026-10-24T09:00:00Z").toLocaleString()}`)).toBeTruthy();
    expect(screen.getByText("unavailable · refreshed never")).toBeTruthy();
    expect(screen.getByText("codex is not installed")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Refresh models" }));
    expect(mocks.refreshModels).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("claude-opus-5-5")).toBeTruthy();
    expect(screen.queryByText("claude-fable-5-1")).toBeNull();
  });

  it("removes a model with the x button and adds one by name", async () => {
    const user = userEvent.setup();
    const edited = {
      runners: [
        { ...testCatalog.runners[0], models: [{ runner: "claude", model: "my-model", display: null, source: "manual" as const }] },
        testCatalog.runners[1],
      ],
    };
    mocks.editModels.mockResolvedValue(edited);
    wrap(<SettingsView />);
    await user.click(await screen.findByRole("button", { name: "Remove claude-fable-5-1" }));
    expect(mocks.editModels).toHaveBeenCalledWith({ runner: "claude", delete: ["claude-fable-5-1"] });
    expect(await screen.findByText("my-model")).toBeTruthy();
    expect(screen.queryByText("claude-sonnet-5-5")).toBeNull();
    await user.type(screen.getByRole("textbox", { name: "Add a model to claude" }), "  new-one ");
    await user.click(screen.getAllByRole("button", { name: "Add" })[0]);
    expect(mocks.editModels).toHaveBeenLastCalledWith({ runner: "claude", add: ["new-one"] });
    expect((screen.getByRole("textbox", { name: "Add a model to claude" }) as HTMLInputElement).value).toBe("");
  });

  it("shows every runner, mounts and scheduler", async () => {
    mocks.runnerStatus.mockResolvedValue({
      claude: { available: true, version: "2.1.309", authenticated: true },
      codex: { available: true, version: "0.163.0", authenticated: false },
      backends: [{ name: "lmstudio", credentials: false }, { name: "zai", credentials: true }],
    });
    wrap(<SettingsView />);
    const runners = await screen.findByRole("list", { name: "Runners" });
    expect(within(runners).getAllByRole("listitem").map(li => li.textContent)).toEqual([
      "claude 2.1.309 · logged in",
      "codex 0.163.0 · not logged in (run codex login --device-auth)",
      "ccx:lmstudio · no credentials in its profile",
      "ccx:zai · credentials set",
    ]);
    expect(await screen.findByText("/data/cfg")).toBeTruthy();
    expect(screen.getByText("/home/u -> /mnt/u")).toBeTruthy();
    expect(screen.getByText(/last tick never · every 30 s/)).toBeTruthy();
    expect(await screen.findByText(/weekly-review/)).toBeTruthy();
    for (const name of ["Runners", "Mounts", "Scheduler", "Connectors"]) expect(screen.getByRole("heading", { name })).toBeTruthy();
  });
});
