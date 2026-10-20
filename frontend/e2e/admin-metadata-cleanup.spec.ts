import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Route } from '@playwright/test';

// Metadata clean-up (#2438, Discussion #2481): rules, preview, apply, undo.
// The admin API is stubbed; the page under test is the real SPA route.

const values = [
  { name: 'Fiction / Thrillers', count: 3, covered: false, status: 'unapproved' },
  { name: 'thriller', count: 2, covered: false, status: 'unapproved' },
  { name: 'General', count: 2, covered: false, status: 'unapproved' },
  { name: 'Dragons', count: 1, covered: false, status: 'unapproved' },
];

const emptyState = {
  field: 'tags', multi: true, approved_only: false, approved: [], rules: [], values, changes: [], history: [],
  files_rewritten: true,
};

const change = {
  book_id: 41, field: 'tags', title: 'Gone Tomorrow', authors: ['Lee Child'],
  before: ['Fiction / Thrillers', 'General'], after: ['Thriller'],
};

test.use({ contextOptions: { reducedMotion: 'reduce' } });

async function stubCleanup(page: Page) {
  const sent: { saved?: unknown; applied?: unknown } = {};
  let state: Record<string, unknown> = { ...emptyState };
  const json = (route: Route, body: unknown) => route.fulfill({ json: body });

  await page.route(/\/api\/v1\/admin\/value-maps\/tags$/, async (route) => {
    if (route.request().method() === 'PUT') {
      sent.saved = route.request().postDataJSON();
      state = {
        ...state,
        rules: [{ incoming: 'Fiction / Thrillers', targets: ['Thriller'] }],
        values: values.map((v) => (v.name === 'Fiction / Thrillers' ? { ...v, covered: true, status: 'renamed' } : v)),
        changes: [change],
      };
    }
    await json(route, state);
  });
  await page.route(/\/api\/v1\/admin\/value-maps\/tags\/preview$/, (route) => json(route, {
    id: 7, source: 'cleanup', status: 'proposed', created_at: '2026-10-19T16:00:00', applied_at: null,
    counts: { proposed: 1 }, changes: [{ ...change, id: 1, status: 'proposed', error: null }],
  }));
  await page.route(/\/api\/v1\/admin\/change-sets\/7\/apply$/, async (route) => {
    sent.applied = route.request().postDataJSON();
    state = {
      ...state,
      changes: [],
      history: [{ id: 7, source: 'cleanup', status: 'applied', created_at: '2026-10-19T16:00:00',
        applied_at: '2026-10-19T16:00:05', counts: { applied: 1 } }],
    };
    await json(route, {
      written: 1, removed_values: ['Fiction / Thrillers'], files_rewritten: true,
      set: { id: 7, source: 'cleanup', status: 'applied', created_at: '2026-10-19T16:00:00',
        applied_at: '2026-10-19T16:00:05', counts: { applied: 1 },
        changes: [{ ...change, id: 1, status: 'applied', error: null }] },
      state,
    });
  });
  return sent;
}

async function assertNoSeriousAxeViolations(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze();
  expect(results.violations.filter((violation) => (
    ['critical', 'serious'].includes(violation.impact || '')
  ))).toEqual([]);
}

test('a rule is added, saved, previewed and applied, with undo offered after', async ({ page }) => {
  const sent = await stubCleanup(page);
  await page.goto('/app/admin/metadata-cleanup');
  await expect(page.getByRole('heading', { name: 'Metadata clean-up' })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Tags' })).toHaveAttribute('aria-selected', 'true');

  // By name, folded the way rules match, so near duplicates sit together.
  await page.getByLabel('Sort values').selectOption('name');
  await expect(page.getByText('Fiction / Thrillers', { exact: true })).toBeVisible();
  const names = await page.locator('[class*="valueName"]').allInnerTexts();
  expect(names.map((n) => n.trim())).toEqual(['Dragons', 'Fiction / Thrillers', 'General', 'thriller']);

  // "Add rule" on a value fills the editor; the unsaved bar sits above the list.
  await page.locator('[class*="value"]').filter({ hasText: 'Fiction / Thrillers' })
    .getByRole('button', { name: 'Add rule' }).click();
  await expect(page.getByLabel('Incoming value')).toHaveValue('Fiction / Thrillers');
  await page.getByLabel(/Becomes/).fill('Thriller');
  await page.getByRole('button', { name: 'Add rule' }).first().click();
  const saveBar = page.getByText('Unsaved changes to the rules.');
  await expect(saveBar).toBeVisible();
  const barBox = await saveBar.boundingBox();
  const ruleBox = await page.locator('[class*="rule"]').filter({ hasText: 'Fiction / Thrillers' }).first().boundingBox();
  expect(barBox && ruleBox && barBox.y < ruleBox.y).toBe(true);

  await page.getByRole('button', { name: 'Save rules' }).click();
  await expect(page.getByText('Rules saved. The preview below is up to date.')).toBeVisible();
  expect(sent.saved).toEqual({ rules: [{ incoming: 'Fiction / Thrillers', targets: ['Thriller'] }] });
  await expect(page.getByRole('link', { name: 'Gone Tomorrow' })).toBeVisible();

  await page.getByRole('button', { name: 'Apply to 1 books' }).click();
  await page.getByRole('button', { name: 'Apply', exact: true }).click();
  await expect(page.getByText(/Changed 1 books\./)).toBeVisible();
  expect(sent.applied).toEqual({ exclude_book_ids: [] });
  await expect(page.getByRole('button', { name: 'Undo' })).toBeVisible();

  for (const theme of ['light', 'dark'] as const) {
    await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
    await assertNoSeriousAxeViolations(page);
  }
});

test('values can be approved, and the only-approved switch is sent as set', async ({ page }) => {
  const sent: { approve?: unknown; settings?: unknown } = {};
  let state: Record<string, unknown> = { ...emptyState };
  await page.route(/\/api\/v1\/admin\/value-maps\/tags$/, (route) => route.fulfill({ json: state }));
  await page.route(/\/api\/v1\/admin\/value-maps\/tags\/approve$/, async (route) => {
    sent.approve = route.request().postDataJSON();
    state = { ...state, approved: ['Dragons'],
      values: values.map((v) => (v.name === 'Dragons' ? { ...v, covered: true, status: 'approved' } : v)) };
    await route.fulfill({ json: { ...state, skipped: [] } });
  });
  await page.route(/\/api\/v1\/admin\/value-maps\/tags\/settings$/, async (route) => {
    sent.settings = route.request().postDataJSON();
    state = { ...state, approved_only: true };
    await route.fulfill({ json: state });
  });
  await page.goto('/app/admin/metadata-cleanup');
  await page.locator('[class*="value"]').filter({ hasText: 'Dragons' }).getByRole('button', { name: 'Approve' }).click();
  expect(sent.approve).toEqual({ names: ['Dragons'] });
  await expect(page.getByText('Approved 1 values.')).toBeVisible();
  // Dragons is decided now, so the default "not decided yet" list no longer shows it.
  await expect(page.locator('[class*="valueName"]').filter({ hasText: 'Dragons' })).toHaveCount(0);
  await page.getByLabel('Only approved values').check();
  expect(sent.settings).toEqual({ approved_only: true });
  await expect(page.getByText(/^On: a value that is not approved/)).toBeVisible();
});
