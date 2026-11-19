/**
 * Eager loading, proved in the production build: after sign-in the browser
 * downloads every screen the person can open, and each screen downloads its
 * forms as soon as it paints - so no click waits on the network.
 *
 * The API is answered by fixtures (one identity, empty lists), so this needs no
 * seeded data and writes nothing.
 */
import { expect, test, type Page } from "@playwright/test";

import { BASE_URL } from "../lib/env.mjs";

test.use({ storageState: { cookies: [], origins: [] } });

const PERMISSIONS = [
  "item.view",
  "item.create",
  "customer.view",
  "user.view",
  "user.create",
  "role.view",
];

/** Answer every API read from fixtures and record any write. */
async function fixtureApi(page: Page, writes: string[]): Promise<void> {
  await page
    .context()
    .addCookies([
      { name: "atcraft_csrf", value: "eager-loading-test", url: BASE_URL },
    ]);
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    if (request.method() !== "GET") writes.push(`${request.method()} ${path}`);
    const data =
      path === "/auth/me"
        ? {
            user_id: "01a00000-0000-7000-8000-000000000001",
            company_id: "01a00000-0000-7000-8000-0000000000c0",
            company_name: "Loading test",
            email: "loading@example.test",
            full_name: "Loading test",
            is_superuser: false,
            must_change_password: false,
            mfa_enabled: true,
            mfa_enrolment_required: false,
            roles: [],
            permissions: PERMISSIONS,
          }
        : [];
    await route.fulfill({
      json: {
        success: true,
        data,
        message: "Data loaded",
        pagination: {
          page: 1,
          limit: 25,
          total: 0,
          total_pages: 0,
          has_next: false,
          has_previous: false,
        },
      },
    });
  });
}

/** Every JavaScript file the page requests, by path. */
function recordScripts(page: Page): string[] {
  const scripts: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.endsWith(".js")) scripts.push(path);
  });
  return scripts;
}

test("every screen a person can open is downloaded after sign-in", async ({
  page,
}) => {
  const writes: string[] = [];
  await fixtureApi(page, writes);
  const scripts = recordScripts(page);

  await page.goto(`${BASE_URL}/`);
  // The shell downloads every permitted screen in the background - the sidebar's,
  // then profile and notifications - without anyone hovering or clicking.
  for (const screen of [
    "items-page",
    "customers-page",
    "users-page",
    "profile-page",
    "notifications-page",
  ]) {
    await expect
      .poll(() => scripts.some((path) => path.includes(`/${screen}-`)))
      .toBe(true);
  }

  const before = scripts.length;
  for (const [link, route] of [
    ["Items", "/items"],
    ["Customers", "/customers"],
    ["Users & Roles", "/admin/users"],
  ] as const) {
    await page.getByRole("link", { name: link, exact: true }).first().click();
    await expect(page).toHaveURL(`${BASE_URL}${route}`);
    await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
  }
  // Opening them downloaded no screen: each rendered from what was already in
  // the browser. (Each screen then fetches its own forms in the background,
  // which the form tests below cover.)
  const screensFetchedOnClick = scripts
    .slice(before)
    .filter((path) => /\/assets\/[\w-]+-page-[\w-]+\.js$/.test(path));
  expect(screensFetchedOnClick).toEqual([]);
  expect(writes).toEqual([]);
});

const screens = [
  { route: "/items", module: "item-form-dialog", button: "New item" },
  { route: "/admin/users", module: "user-dialogs", button: "New user" },
] as const;

for (const screen of screens) {
  test(`${screen.route} downloads its form right after it paints`, async ({
    page,
  }) => {
    const writes: string[] = [];
    await fixtureApi(page, writes);
    const scripts = recordScripts(page);
    const formDownloads = () =>
      scripts.filter((path) => path.includes(screen.module)).length;

    await page.goto(`${BASE_URL}${screen.route}`);
    const open = page
      .getByRole("button", { name: screen.button, exact: true })
      .first();
    await expect(open).toBeVisible();
    // Before anyone clicks, the form is already downloading or downloaded.
    await expect.poll(formDownloads).toBeGreaterThan(0);
    const downloaded = formDownloads();

    await open.click();
    const dialog = page.getByRole("dialog", {
      name: screen.button,
      exact: true,
    });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await open.click();
    await expect(dialog).toBeVisible();
    // Opening and reopening it fetched nothing more.
    expect(formDownloads()).toBe(downloaded);
    expect(writes).toEqual([]);
  });
}
