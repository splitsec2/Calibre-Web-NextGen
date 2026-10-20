import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

// Tags page "Also add a rule" (Discussion #2481, #2438): a rename or delete is a
// one-off unless an admin ticks the box, which then sends add_rule and says
// which rule was added. The tag API is stubbed; the page is the real SPA route,
// signed in as the e2e admin.

const tags = [
  { id: 101, name: 'Fiction / Thrillers', count: 3 },
  { id: 102, name: 'General', count: 2 },
];

test.use({ contextOptions: { reducedMotion: 'reduce' } });

async function stubTags(page: Page) {
  const sent: { rename?: unknown; deleteUrl?: string } = {};
  await page.route(/\/api\/v1\/tags$/, (route) => route.fulfill({ json: { items: tags } }));
  await page.route(/\/api\/v1\/tags\/101$/, async (route) => {
    sent.rename = route.request().postDataJSON();
    const body = sent.rename as { name: string; add_rule?: boolean };
    await route.fulfill({ json: {
      id: 101, name: body.name,
      ...(body.add_rule ? { rule: { incoming: 'Fiction / Thrillers', targets: [body.name] } } : {}),
    } });
  });
  await page.route(/\/api\/v1\/tags\/102(\?.*)?$/, async (route) => {
    sent.deleteUrl = route.request().url();
    await route.fulfill({ json: { id: 102, name: 'General', deleted: true, books: 2 } });
  });
  return sent;
}

test('an admin rename adds a rule only when the box is ticked, and says which', async ({ page }) => {
  const sent = await stubTags(page);
  await page.goto('/app/tags');
  await page.getByRole('button', { name: 'Rename tag Fiction / Thrillers' }).click();

  const box = page.getByLabel('Also add a rule in Metadata clean-up');
  await expect(box).toBeVisible();
  await expect(box).not.toBeChecked();
  await box.check();
  await page.getByLabel('Tag name').fill('Thriller');
  await page.getByRole('button', { name: 'Save tag name' }).click();

  expect(sent.rename).toEqual({ name: 'Thriller', add_rule: true });
  await expect(page.getByText('Rule added: “Fiction / Thrillers” becomes “Thriller” when the clean-up rules are applied.'))
    .toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Metadata clean-up' })).toHaveAttribute('href', /admin\/metadata-cleanup/);

  for (const theme of ['light', 'dark'] as const) {
    await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze();
    expect(results.violations.filter((v) => ['critical', 'serious'].includes(v.impact || ''))).toEqual([]);
  }
});

test('a delete with the box left unticked sends no rule', async ({ page }) => {
  const sent = await stubTags(page);
  await page.goto('/app/tags');
  await page.getByRole('button', { name: 'Delete tag General' }).click();
  await expect(page.getByText('Delete “General”? It is removed from 2 books, which are kept.')).toBeVisible();
  await expect(page.getByLabel('Also add a rule in Metadata clean-up')).not.toBeChecked();
  await page.getByRole('button', { name: 'Confirm delete tag General' }).click();
  await expect.poll(() => sent.deleteUrl).toMatch(/\/api\/v1\/tags\/102$/);
  await expect(page.getByText(/Rule added/)).toHaveCount(0);
});

test('the Publishers page renames with the same box and offers no delete', async ({ page }) => {
  let sent: unknown;
  await page.route(/\/api\/v1\/publishers$/, (route) => route.fulfill({ json: { items: [
    { id: 201, name: 'Penguin Books Ltd', count: 4 },
  ] } }));
  await page.route(/\/api\/v1\/publishers\/201$/, async (route) => {
    sent = route.request().postDataJSON();
    await route.fulfill({ json: { id: 201, name: 'Penguin', rule: { incoming: 'Penguin Books Ltd', targets: ['Penguin'] } } });
  });
  await page.goto('/app/publishers');
  await expect(page.getByRole('button', { name: /Delete publisher/ })).toHaveCount(0);
  await page.getByRole('button', { name: 'Rename publisher Penguin Books Ltd' }).click();
  await page.getByLabel('Also add a rule in Metadata clean-up').check();
  await page.getByLabel('Publisher name').fill('Penguin');
  await page.getByRole('button', { name: 'Save publisher name' }).click();
  expect(sent).toEqual({ name: 'Penguin', add_rule: true });
  await expect(page.getByText('Rule added: “Penguin Books Ltd” becomes “Penguin” when the clean-up rules are applied.'))
    .toBeVisible();
});
