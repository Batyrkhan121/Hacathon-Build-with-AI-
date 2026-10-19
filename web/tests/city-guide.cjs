/* Integration regression checks for the production web app.
 * Start the repository's static server, then run:
 * PLAYWRIGHT_MODULE=/path/to/playwright node web/tests/city-guide.cjs
 * These checks intercept API responses; they do not call an LLM or send GPS off-device.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.QADAM_WEB_URL || 'http://127.0.0.1:5513/web/';
const landmarks = JSON.parse(fs.readFileSync('data/landmarks.json', 'utf8'));
const answer = JSON.parse(fs.readFileSync('web/mock/answer_route.json', 'utf8'));
const results = [];
async function check(name, fn) {
  try { await fn(); results.push({ name, pass: true }); }
  catch (err) { results.push({ name, pass: false, error: err.message }); }
}
async function noOverflow(page) {
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
}
async function setup(browser, width = 390, lang = 'ru') {
  const context = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, reducedMotion: 'reduce', permissions: ['geolocation'], geolocation: { latitude: 51.128376, longitude: 71.430123, accuracy: 10 } });
  await context.addInitScript(lang => localStorage.setItem('qadam.lang', lang), lang);
  const page = await context.newPage();
  const errors = []; page.on('pageerror', err => errors.push(err.message));
  let response = answer, status = 200, delay = 0;
  const requests = [];
  await page.route('**/api/landmarks', route => route.fulfill({ json: landmarks }));
  await page.route('**/api/meta', route => route.fulfill({ json: {} }));
  await page.route('**/api/recommend', async route => {
    requests.push(route.request().postDataJSON());
    if (delay) await new Promise(resolve => setTimeout(resolve, delay));
    try { await route.fulfill({ status, json: status === 200 ? response : { detail: 'test error' } }); } catch { /* aborted request */ }
  });
  await page.goto(base);
  await page.locator('#landmark option').last().waitFor({ state: 'attached' });
  return { page, context, requests, errors, respond(value, code = 200, ms = 0) { response = value; status = code; delay = ms; } };
}
async function submit(page) {
  await page.locator('#query').fill('Прогулка на час рядом с EXPO');
  await page.locator('#submit').click();
  await page.locator('.card:not(.skeleton)').last().waitFor();
}
(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'chrome' });
  for (const width of [320, 390, 768, 999, 1000, 1440]) {
    for (const lang of ['ru', 'kk', 'en']) {
      const t = await setup(browser, width, lang), p = t.page;
      await check(`${width} ${lang}: home and localized heading`, async () => {
        await noOverflow(p);
        assert.equal(await p.locator('.hero-tagline').evaluate(e => e.getBoundingClientRect().height < parseFloat(getComputedStyle(e).fontSize) * 1.5), true);
        const button = await p.locator('#submit').boundingBox();
        assert.ok(button.y + button.height <= (width < 600 ? 844 : 900));
        assert.equal(await p.locator('.site-header .logo path').getAttribute('d'), 'M6 24L14 8M14 24L22 8');
      });
      await check(`${width} ${lang}: API request and results`, async () => {
        await submit(p); await noOverflow(p);
        assert.equal(t.requests[0].lang, lang);
        assert.deepEqual(t.requests[0].location, { landmark_id: 'expo' });
        assert.equal(t.requests[0].overrides, null);
        assert.equal(t.requests[0].want_route, true);
        assert.equal(t.requests[0].use_llm, true);
        assert.equal(await p.locator('.card:not(.skeleton)').count(), answer.cards.length);
        assert.ok((await p.locator('.answer-count').innerText()).includes(String(answer.route.total_min)));
        assert.equal(await p.locator('.directions a').count(), answer.cards.length * 2);
        const source = answer.cards[0];
        assert.equal(await p.locator('#card-1 .stop-reason dd').innerText(), source.why);
        await p.locator('#card-1 .card-details summary').focus(); await p.keyboard.press('Enter');
        assert.equal(await p.locator('#card-1 .card-details').evaluate(e => e.open), true);
        assert.ok((await p.locator('#card-1 .card-more-text').innerText()).includes(source.what_to_do));
        await p.locator('#card-1 .card-details summary').click();
        await p.locator('.route summary').click();
        assert.equal(await p.locator('.route').evaluate(e => e.open), true);
        assert.equal(await p.locator('.route-graph-line').count(), 1);
        assert.ok(await p.locator('.line-legend').isVisible());
        assert.ok(await p.locator('.route-step').count() > 0);
        assert.deepEqual(t.errors, []);
      });
      await check(`${width} ${lang}: map and edit round trip`, async () => {
        await p.locator('#card-2 .card-actions button').first().click();
        if (width < 1000) {
          assert.ok(await p.locator('#map-close').isVisible());
          assert.equal(await p.evaluate(() => document.activeElement.id), 'map-close');
          await p.locator('#back-to-card').click();
          assert.equal(await p.locator('body').evaluate(e => e.classList.contains('map-open')), false);
          assert.equal(await p.evaluate(() => document.activeElement.id), 'card-2');
        }
        await p.locator('#query-summary').click();
        assert.equal(await p.locator('#query').inputValue(), 'Прогулка на час рядом с EXPO');
        await p.locator('#budget-toggle').click();
        await p.locator('#budget-chips button').first().click();
        await p.locator('#refine-done').click();
        await p.locator('#edit-return').click();
        assert.ok(await p.locator('#stale').isVisible());
        await p.locator('#stale-btn').click();
        await p.locator('.card:not(.skeleton)').last().waitFor();
        assert.equal(t.requests.at(-1).overrides.time_budget_min, 30);
        await noOverflow(p); assert.deepEqual(t.errors, []);
      });
      await t.context.close();
    }
  }
  for (const width of [390, 1440]) {
    const t = await setup(browser, width), p = t.page;
    await check(`${width}: loading cancel and language change`, async () => {
      t.respond(answer, 200, 1000);
      await p.locator('#presets button').first().click();
      await p.locator('.loading-cancel').waitFor();
      await p.locator('[data-lang=en]').click();
      assert.equal(await p.locator('.loading-cancel').innerText(), 'Cancel');
      await p.locator('.loading-cancel').click();
      assert.ok(await p.locator('#query').isVisible());
      assert.equal(await p.locator('.loading').count(), 0);
    });
    await check(`${width}: error and retry without AI`, async () => {
      t.respond(answer, 500); await p.locator('#submit').click(); await p.locator('.error-box').waitFor();
      t.respond(answer); await p.locator('.error-actions .btn-primary').click();
      await p.locator('.card:not(.skeleton)').last().waitFor();
      assert.equal(t.requests.at(-1).use_llm, false);
    });
    await check(`${width}: empty and return to request`, async () => {
      await p.locator('#query-summary').click();
      t.respond({ ...answer, cards: [], route: null }); await p.locator('#submit').click();
      await p.locator('.empty').waitFor();
      await p.locator('.empty-actions button').last().click();
      assert.ok(await p.locator('#query').isVisible());
    });
    await check(`${width}: GPS remains a resolution 10 cell`, async () => {
      await p.locator('#where-toggle').click(); await p.locator('#geo-btn').click();
      await p.waitForFunction(() => document.querySelector('#landmark').value === '__cell');
      await p.locator('#where-done').click(); t.respond(answer);
      await p.locator('#submit').click(); await p.locator('.card:not(.skeleton)').last().waitFor();
      const loc = t.requests.at(-1).location;
      assert.deepEqual(Object.keys(loc), ['h3_cell']);
      assert.equal(await p.evaluate(cell => h3.getResolution(cell), loc.h3_cell), 10);
      assert.ok(!JSON.stringify(t.requests.at(-1)).includes('51.128376'));
      assert.ok(!JSON.stringify(t.requests.at(-1)).includes('71.430123'));
      const links = await p.locator('.directions a').evaluateAll(els => els.map(e => e.href).join(' '));
      assert.ok(!links.includes('51.128376'));
      assert.deepEqual(t.errors, []);
    });
    await t.context.close();
  }
  for (const width of [390, 1440]) {
    const t = await setup(browser, width), p = t.page;
    await check(`${width}: route subset, order and loop membership`, async () => {
      const subset = structuredClone(answer);
      subset.route.stop_ids = [answer.cards[2].poi_id];
      subset.route.legs = [
        { from_id: 'origin', to_id: answer.cards[2].poi_id, distance_m: 100, walk_min: 2 },
        { from_id: answer.cards[2].poi_id, to_id: 'origin', distance_m: 100, walk_min: 2 },
      ];
      subset.route.fits_budget = false;
      t.respond(subset); await submit(p);
      assert.equal(await p.locator('.routed-cards .card').count(), 1);
      assert.equal(await p.locator('.routed-cards .card').first().getAttribute('id'), 'card-3');
      assert.equal(await p.locator('.alternatives .card').count(), 2);
      assert.ok((await p.locator('.answer-count').innerText()).includes('Остановок: 1'));
      assert.equal(await p.locator('.walk-overview').count(), 0);
      assert.ok(await p.locator('.answer-head > .route-over').isVisible());
      assert.notEqual(await p.locator('.alternatives .rank').first().evaluate(e => getComputedStyle(e).backgroundColor), 'rgb(242, 183, 5)');
      assert.equal(await p.locator('.stop-icon.is-first').count(), 1);
      const ordered = structuredClone(answer);
      ordered.route.stop_ids = [answer.cards[2].poi_id, answer.cards[0].poi_id];
      t.respond(ordered); await p.locator('#query-summary').click(); await p.locator('#submit').click();
      await p.locator('.card:not(.skeleton)').last().waitFor();
      assert.deepEqual(await p.locator('.routed-cards .card').evaluateAll(els => els.map(e => e.id)), ['card-3', 'card-1']);
      assert.deepEqual(await p.locator('.walk-stop-number').allTextContents(), ['3', '1']);
    });
    await check(`${width}: straight estimate and route-less direct fallback`, async () => {
      const straight = structuredClone(answer); straight.route.path_source = 'straight';
      t.respond(straight); await p.locator('#query-summary').click(); await p.locator('#submit').click();
      await p.locator('.card:not(.skeleton)').last().waitFor();
      assert.ok(await p.locator('path.leaflet-interactive[stroke-dasharray]').count() > 0);
      assert.equal(await p.locator('.line-swatch.is-solid').count(), 0);
      t.respond({ ...answer, route: null }); await p.locator('#query-summary').click(); await p.locator('#submit').click();
      await p.locator('.card:not(.skeleton)').last().waitFor();
      assert.equal(await p.locator('.result-heading h2').innerText(), 'Места рядом');
      assert.equal(await p.locator('.walk-overview').count(), 0);
      assert.ok(!(await p.locator('.answer-count').innerText()).includes(String(answer.route.total_min)));
      await p.locator('#card-1 .card-actions button').last().click();
      assert.ok(await p.locator('path.leaflet-interactive[stroke-dasharray]').count() > 0);
      assert.equal(await p.locator('.line-swatch.is-solid').count(), 0);
      if (width < 1000) {
        assert.equal(await p.evaluate(() => document.activeElement.id), 'map-close');
        await p.locator('#map-close').click();
      }
      await noOverflow(p); assert.deepEqual(t.errors, []);
    });
    await t.context.close();
  }
  await browser.close();
  const failed = results.filter(x => !x.pass);
  console.log(JSON.stringify({ passed: results.length - failed.length, total: results.length, failed }, null, 2));
  process.exitCode = failed.length ? 1 : 0;
})().catch(err => { console.error(err); process.exitCode = 1; });
