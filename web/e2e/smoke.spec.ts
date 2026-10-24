import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

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
