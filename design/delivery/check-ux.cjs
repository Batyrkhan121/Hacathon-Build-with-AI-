/* Read-only interaction checks for the designer preview. No backend calls.
 * Run: QADAM_PREVIEW_URL=http://127.0.0.1:5512/demo/ node check-ux.cjs
 * Install playwright locally, or set PLAYWRIGHT_MODULE to its module path.
 */
const assert = require('node:assert/strict');
function loadPlaywright() {
  const candidates = [process.env.PLAYWRIGHT_MODULE, 'playwright'].filter(Boolean);
  for (const name of candidates) {
    try { return require(name); } catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; }
  }
  throw new Error('Install playwright or set PLAYWRIGHT_MODULE.');
}
const { chromium } = loadPlaywright();
const base = process.env.QADAM_PREVIEW_URL || 'http://127.0.0.1:5512/demo/';
const records = [];
async function check(label, work) {
  try { const detail = await work(); records.push({ label, pass: true, detail }); }
  catch (error) { records.push({ label, pass: false, error: error.message }); }
}
async function ready(page, view = 'home', lang = 'kk') {
  const url = new URL(base); url.searchParams.set('view', view); url.searchParams.set('lang', lang);
  await page.goto(url.href); await page.waitForFunction(() => window.preview?.ready);
}
async function answerReady(page) {
  await page.waitForFunction(() => preview.state.view !== 'loading' && !!preview.state.answer);
}
async function noOverflow(page) {
  const sizes = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth }));
  assert.ok(sizes.document <= sizes.viewport + 1, JSON.stringify(sizes));
  return sizes;
}
(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' });
  for (const width of [360, 1280]) {
    const page = await browser.newPage({ viewport: { width, height: width === 360 ? 800 : 720 } });
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    const label = name => `${width}px ${name}`;
    await check(label('home visible controls / overflow'), async () => {
      await ready(page); assert.equal(await page.locator('#presets .preset').count(), 5);
      const button = await page.locator('#submit').boundingBox();
      assert.ok(button && button.y + button.height <= (width === 360 ? 800 : 720));
      return noOverflow(page);
    });
    await check(label('submit → answer → keyboard details'), async () => {
      await page.locator('#query').fill('EXPO маңында бір сағат серуен');
      await page.locator('#submit').click(); await answerReady(page);
      assert.ok(await page.locator('.card:not(.skeleton)').count() > 0);
      const summary = page.locator('.card-details summary').first(); await summary.focus(); await page.keyboard.press('Enter');
      assert.equal(await page.locator('.card-details').first().evaluate(el => el.open), true);
      await page.keyboard.press('Enter'); assert.equal(await page.locator('.card-details').first().evaluate(el => el.open), false);
      return noOverflow(page);
    });
    await check(label('map / return'), async () => {
      await page.locator('.card-actions button').first().click();
      if (width === 360) {
        assert.equal(await page.evaluate(() => preview.state.mapOpen), true);
        assert.equal(await page.locator('.map-back').isVisible(), true);
        await page.locator('.map-back').click();
        assert.equal(await page.evaluate(() => preview.state.mapOpen), false);
        assert.equal(await page.evaluate(() => document.activeElement.closest('.card-actions') !== null), true);
      } else assert.equal(await page.locator('.map-wrap').isVisible(), true);
      return noOverflow(page);
    });
    await check(label('edit → resubmit'), async () => {
      await page.locator('.query-summary').click(); await page.locator('#query').fill('Жаңа серуен сұранысы');
      await page.locator('#submit').click(); await answerReady(page);
      assert.equal(await page.locator('.query-summary strong').innerText(), 'Жаңа серуен сұранысы');
      assert.equal(await page.evaluate(() => preview.state.editing), false);
      return noOverflow(page);
    });
    await check(label('edit cancellation restores submitted request'), async () => {
      const prior = await page.evaluate(() => preview.request());
      await page.locator('.query-summary').click(); await page.locator('#query').fill('Unsaved draft');
      await page.locator('.parameters summary').click(); await page.locator('#budget-chips button').first().click();
      await page.locator('.edit-cancel').click();
      assert.deepEqual(await page.evaluate(() => preview.request()), prior);
      assert.equal(await page.locator('#stale').isVisible(), false);
    });
    await check(label('loading cancellation'), async () => {
      await ready(page); await page.locator('#query').fill('Cancel this loading'); await page.locator('#submit').click();
      await page.locator('.loading-cancel').click(); await page.waitForTimeout(900);
      assert.equal(await page.evaluate(() => preview.state.view), 'home');
      assert.equal(await page.locator('#query').inputValue(), 'Cancel this loading');
      return noOverflow(page);
    });
    if (width === 360) await check(label('pick-map Back cancels and restores focus'), async () => {
      await ready(page); await page.locator('.parameters summary').click(); await page.locator('#pick-btn').click();
      assert.equal(await page.locator('#map').evaluate(el => el.classList.contains('is-picking')), true);
      await page.locator('.map-back').click();
      assert.equal(await page.locator('#map').evaluate(el => el.classList.contains('is-picking')), false);
      assert.equal(await page.locator('.pick-banner').count(), 0);
      assert.equal(await page.evaluate(() => document.activeElement.id), 'pick-btn');
    });
    for (const [preset, expectedView] of Object.entries({ S1: 'answer-short', S2: 'coffee', S3: 'kids', S4: 'departure', S5: 'night' })) {
      await check(label(`preset ${preset}`), async () => {
        await ready(page); await page.locator(`[data-preset="${preset}"]`).click(); await answerReady(page);
        assert.equal(await page.evaluate(() => preview.state.view), expectedView);
        const context = await page.evaluate(() => ({ request: preview.request(), cards: preview.state.answer.cards.length }));
        assert.deepEqual(Object.keys(context.request.location), ['landmark_id']);
        if (preset === 'S3') assert.equal(context.request.overrides.with_kids, true);
        if (preset === 'S4') assert.equal(context.request.overrides.departure_in_min, 120);
        if (preset === 'S5') assert.equal(context.request.overrides.night, true);
        await noOverflow(page); return { cards: context.cards, location: context.request.location };
      });
    }
    for (const lang of ['kk', 'ru', 'en']) await check(label(`language ${lang} / trusted hours`), async () => {
      await ready(page, 'answer', lang);
      const firstTitle = await page.locator('.card-title').first().innerText();
      assert.equal(await page.locator(`[data-lang="${lang}"]`).getAttribute('aria-pressed'), 'true');
      assert.equal(await page.evaluate(() => preview.request().lang), lang);
      const invalidGreen = await page.evaluate(() => [...document.querySelectorAll('.card')].filter(el => el.querySelector('.badge-hours.is-open')).map(el => preview.state.answer.cards.find(card => card.rank === +el.dataset.rank)).filter(card => card.open_status !== 'open' || card.open_confidence !== 'high'));
      assert.equal(invalidGreen.length, 0); await noOverflow(page); return firstTitle;
    });
    await check(label('unknown and closed-medium hours stay uncertain'), async () => {
      await ready(page, 'card-unnamed'); assert.equal(await page.locator('.badge-hours.is-open').count(), 0);
      assert.equal(await page.locator('.badge-hours.is-unknown').count(), 1);
      await ready(page, 'closed-medium'); assert.equal(await page.locator('.badge-hours.is-open').count(), 0);
      assert.ok(await page.locator('.badge-hours.is-medium').count() > 0);
      return page.locator('.badge-hours').first().innerText();
    });
    await check(label('JavaScript errors'), () => { assert.deepEqual(errors, []); });
    await page.close();
  }
  await browser.close();
  console.log(JSON.stringify({ checks: records.length, passed: records.filter(r => r.pass).length, failed: records.filter(r => !r.pass).length, records }, null, 2));
  if (records.some(record => !record.pass)) process.exitCode = 1;
})().catch(error => { console.error(error); process.exitCode = 1; });
