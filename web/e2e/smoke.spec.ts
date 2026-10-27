import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

// The live model list fails in e2e (no network, or a fake token) and a failed refresh keeps the list it had,
// which is empty here; add the model the steps pick by hand, as the Settings tab does.
async function addFable(page: Page, headers: Record<string, string>) {
  const edited = await page.request.post("/api/models/edit", { headers, data: { runner: "claude", add: ["claude-fable-5-1"] } });
  expect(edited.status()).toBe(200);
}

test("create a project and an event, see the block, revert it from the ledger", async ({ page }) => {
  const token = readFileSync(process.env.TRAJECTREE_DATA_DIR + "/token", "utf8").trim();
  await page.goto(`/?token=${token}`);
  await expect(page).toHaveURL(/\/$/);
  await page.getByRole("button", { name: "+ Project" }).click();
  await page.getByLabel("Slug").fill("clmi");
  await page.getByLabel("Title").fill("CLMI");
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.locator("g.lab", { hasText: "CLMI" })).toBeVisible();
  await page.getByRole("button", { name: "Quick capture" }).click();
  await page.getByLabel("Title").fill("Encoder frozen");
  await page.getByLabel("Kind").fill("decision");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.locator("g.block.k-decision")).toHaveCount(1);
  await page.getByRole("tab", { name: "Ledger" }).click();
  await page.getByText("record event: Encoder frozen").click();
  page.once("dialog", d => d.accept());
  await page.getByRole("button", { name: "Revert" }).first().click();
  await page.getByRole("tab", { name: "Timeline" }).click();
  await expect(page.locator("g.block.k-decision")).toHaveCount(0);
  await page.getByRole("tab", { name: "Inbox" }).click();
  await expect(page.getByText("Nothing to review")).toBeVisible();
  const run = await page.request.post("/api/ingest/run", { headers: { Authorization: `Bearer ${token}` } });
  expect(run.status()).toBe(200);
  await page.getByRole("tab", { name: "Timeline" }).click();
  await expect(page.locator("g.lab", { hasText: "CLMI" })).toBeVisible();

  const cred = await page.request.post("/api/runner/credentials", {
    headers: { Authorization: `Bearer ${token}` },
    data: { oauth_token: "fake" },
  });
  expect(cred.status()).toBe(200);
  // No network in e2e: the live Claude call fails and the catalog falls back to the built-in models.
  const refreshed = await page.request.post("/api/models/refresh", { headers: { Authorization: `Bearer ${token}` } });
  expect(refreshed.status()).toBe(200);
  await addFable(page, { Authorization: `Bearer ${token}` });
  await page.getByRole("button", { name: "Quick capture" }).click();
  await page.getByLabel("Describe it").fill("we froze the encoder");
  await page.getByRole("combobox", { name: "Model", exact: true }).selectOption({ value: "claude|claude-fable-5-1" });
  await page.getByRole("button", { name: "Parse" }).click();
  await expect(page.getByText("Encoder frozen (parsed)")).toBeVisible();
  await page.getByRole("button", { name: "Accept" }).click();
  // The panel closes as soon as the proposal is applied (App's `created`), so the "applied" badge is
  // never stable; the timeline block below proves the apply.
  await page.getByRole("tab", { name: "Timeline" }).click();
  await expect(page.locator("g.block.k-decision")).toHaveCount(1);
  await page.getByRole("tab", { name: "Runs" }).click();
  await expect(page.getByRole("heading", { name: "Run history" })).toBeVisible();
  await expect(page.locator("span.badge.run-ok").first()).toBeVisible();
});

test("write and export a report, run a routine, open settings", async ({ page }) => {
  const token = readFileSync(process.env.TRAJECTREE_DATA_DIR + "/token", "utf8").trim();
  const auth = { Authorization: `Bearer ${token}` };
  await page.goto(`/?token=${token}`);
  await expect(page).toHaveURL(/\/$/);
  const cred = await page.request.post("/api/runner/credentials", { headers: auth, data: { oauth_token: "fake" } });
  expect(cred.status()).toBe(200);
  const refreshed = await page.request.post("/api/models/refresh", { headers: auth });
  expect(refreshed.status()).toBe(200);
  await addFable(page, auth);

  await page.getByRole("tab", { name: "Reports" }).click();
  await page.getByRole("button", { name: "New report" }).click();
  await page.getByLabel("Title").fill("Smoke");
  await page.getByLabel("Kind").selectOption("memo");
  await page.getByRole("button", { name: "Create" }).click();
  const source = page.getByRole("textbox", { name: "Source" });
  await expect(source).toBeVisible();
  await source.click();
  await page.keyboard.type("# Smoke memo");
  const save = page.getByRole("button", { name: "Save report" });
  await expect(save).toBeEnabled();
  await save.click();
  // Saved once the refetched source matches the buffer, which disables the button again.
  await expect(save).toBeDisabled();
  const list = await page.request.get("/api/reports", { headers: auth });
  expect(list.status()).toBe(200);
  const report = (await list.json()).find((r: { title: string }) => r.title === "Smoke");
  expect(report).toBeTruthy();
  const exported = await page.request.get(`/api/reports/${report.id}/export?format=html`, { headers: auth });
  expect(exported.status()).toBe(200);
  expect(await exported.text()).toContain("Smoke memo");

  await page.getByRole("tab", { name: "Runs" }).click();
  await expect(page.getByRole("heading", { name: "Routines" })).toBeVisible();
  const row = page.locator("tr", { hasText: "cross-project-review" });
  await row.getByRole("combobox", { name: "Model", exact: true }).selectOption({ value: "claude|claude-fable-5-1" });
  await row.getByRole("button", { name: "Run now" }).click();
  await expect(row.locator(".badge.run-ok")).toBeVisible();

  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { name: "Tokens" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Models" })).toBeVisible();
  await page.getByRole("button", { name: "Refresh models" }).click();
  await expect(page.getByRole("list", { name: "claude models" }).getByText("claude-fable-5-1")).toBeVisible();
});

test("the Routines tab does not scroll sideways at phone width with a long runner error", async ({ page }) => {
  const token = readFileSync(process.env.TRAJECTREE_DATA_DIR + "/token", "utf8").trim();
  // Add an unavailable runner whose error is far wider than a phone screen.
  await page.route("**/api/models", async route => {
    const res = await route.fetch();
    const body = await res.json();
    body.runners.push({ runner: "codex", available: false, models: [], refreshed_at: null, error: "codex is not installed and ".repeat(8) });
    await route.fulfill({ response: res, json: body });
  });
  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto(`/?token=${token}`);
  await expect(page).toHaveURL(/\/$/);
  await page.getByRole("tab", { name: "Runs" }).click();
  await expect(page.getByRole("heading", { name: "Routines" })).toBeVisible();
  const row = page.locator("tr", { hasText: "cross-project-review" });
  await expect(row.getByRole("combobox", { name: "Model", exact: true })).toBeVisible();
  const widths = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, inner: window.innerWidth }));
  expect(widths.scroll).toBeLessThanOrEqual(widths.inner);
});

test("a sideways wheel pans the timeline; a block's tooltip stays inside the window", async ({ page }) => {
  const token = readFileSync(process.env.TRAJECTREE_DATA_DIR + "/token", "utf8").trim();
  const auth = { Authorization: `Bearer ${token}` };
  const project = await page.request.post("/api/projects", { headers: auth, data: { slug: "bottom-lane", title: "Bottom lane", kind: "research", lane_order: 99 } });
  expect(project.status()).toBe(200);
  const day = new Date(Date.now() - 86_400_000).toISOString();
  const event = await page.request.post("/api/events", {
    headers: auth, data: { project_id: (await project.json()).id, kind: "milestone", title: "Bottom block", t_start: day },
  });
  expect(event.status()).toBe(200);
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto(`/?token=${token}`);
  await expect(page).toHaveURL(/\/$/);
  const block = page.locator("g.block", { hasText: "Bottom block" });
  await expect(block).toBeVisible();

  // Hovering the bottom lane's block: the tooltip is fixed to the window, not clipped by the frame.
  const box = (await block.locator("rect.body").boundingBox())!; // the label beside it takes no pointer
  await page.mouse.move(box.x + box.width / 2, box.y + box.height - 1);
  const tip = page.locator("div.tip");
  await expect(tip).toBeVisible();
  const placed = await tip.evaluate(el => {
    const r = el.getBoundingClientRect();
    return { position: getComputedStyle(el).position, top: r.top, bottom: r.bottom, right: r.right, h: window.innerHeight, w: window.innerWidth };
  });
  expect(placed.position).toBe("fixed");
  expect(placed.top).toBeGreaterThanOrEqual(0);
  expect(placed.bottom).toBeLessThanOrEqual(placed.h);
  expect(placed.right).toBeLessThanOrEqual(placed.w);

  // A horizontal wheel pans the time axis and never scrolls the page.
  const before = await block.getAttribute("transform");
  await page.mouse.wheel(300, 0);
  await expect.poll(() => block.getAttribute("transform")).not.toBe(before);
  expect(await page.evaluate(() => window.scrollX)).toBe(0);
});

test("tabs have paths that survive a refresh; the drawer closes unsaved; a project's kind changes", async ({ page }) => {
  const token = readFileSync(process.env.TRAJECTREE_DATA_DIR + "/token", "utf8").trim();
  const auth = { Authorization: `Bearer ${token}` };
  const created = await page.request.post("/api/projects", { headers: auth, data: { slug: "kind-change", title: "Kind change", kind: "research" } });
  expect(created.status()).toBe(200);
  await page.goto(`/?token=${token}`);
  await expect(page).toHaveURL(/\/$/);

  await page.getByRole("tab", { name: "Runs" }).click();
  await expect(page).toHaveURL(/\/runs$/);
  await page.reload();
  await expect(page.getByRole("tab", { name: "Runs" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("list", { name: "Runners" })).toContainText("codex");
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Runners" })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("tab", { name: "Runs" })).toHaveAttribute("aria-selected", "true");

  await page.getByRole("button", { name: "Quick capture" }).click();
  const drawer = page.getByRole("complementary", { name: "Quick capture" });
  await drawer.getByLabel("Title").fill("never saved");
  await drawer.getByRole("button", { name: "Close" }).click();
  await expect(drawer).toBeHidden();
  await page.getByRole("button", { name: "Ask" }).click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("complementary")).toBeHidden();

  await page.goto("/project/kind-change");
  await expect(page.getByRole("heading", { name: "Kind change" })).toBeVisible();
  await page.getByRole("button", { name: "Edit" }).click();
  await page.getByLabel("Kind").selectOption("tooling");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("tooling · active")).toBeVisible();
  await page.reload();
  await expect(page.getByText("tooling · active")).toBeVisible();
});
