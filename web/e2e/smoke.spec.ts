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
});
