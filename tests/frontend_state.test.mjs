// Run: node --test tests/frontend_state.test.mjs
// Executes the real UI event handlers offline. Only DOM, rendering and transport are faked.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.events = {};
    this.attributes = {};
    this.hidden = false;
    this.value = '';
    this.style = {};
    this.scrollHeight = 48;
    this.classList = { add() {}, remove() {} };
  }
  addEventListener(name, fn) { this.events[name] = fn; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  append(...nodes) { this.children.push(...nodes); }
  prepend(...nodes) { this.children.unshift(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  get firstChild() { return this.children[0]; }
  querySelector(tag) { return this.children.find((node) => node.tagName === tag) || new Element(tag); }
  scrollIntoView() {}
  focus() {}
  click() { return this.events.click?.(); }
  input(value) { this.value = value; this.events.input?.(); }
  change(value) { this.value = value; this.events.change?.(); }
}

const appSource = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8')
  .replace(/^import[^\n]+\n/gm, '')
  .replace(/init\(\);\s*$/, '');

function harness() {
  const elements = new Map();
  const el = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  el('stale').hidden = true;
  const calls = [];
  let lang = 'ru';
  let clock = '15:00';
  let langListener;
  let rendered;
  const context = vm.createContext({
    document: {
      getElementById: el,
      querySelectorAll: () => [],
      createElement: (tag) => new Element(tag),
      addEventListener() {},
    },
    window: { matchMedia: () => ({ matches: true }), addEventListener() {} },
    mapView: new Proxy({}, { get: () => () => {} }),
    view: {
      renderAnswer: (_, answer) => { rendered = { kind: 'answer', answer }; },
      renderLoading: () => { rendered = { kind: 'loading' }; },
      renderError: (_, error) => { rendered = { kind: 'error', error }; },
      renderIntro: () => { rendered = { kind: 'intro' }; },
    },
    t: (key) => key,
    getLang: () => lang,
    onLangChange: (fn) => { langListener = fn; },
    formatNumber: String,
    astanaClock: () => clock,
    shortDay: (day) => String(day),
    atIso: (time, day) => time || day ? `${day}:${time || clock}` : null,
    setInterval() {},
    AbortController,
    performance,
    Option: class extends Element {},
    recommend: (body, signal) => new Promise((resolve, reject) => {
      calls.push({ body: structuredClone(body), signal, resolve, reject });
      signal.addEventListener('abort', () => reject({ kind: 'aborted' }), { once: true });
    }),
  });
  vm.runInContext(`${appSource}\nglobalThis.ui = { submit, cancelRequest }; bindEvents(); renderAll();`, context);
  el('query').input('Walk near EXPO');
  return {
    el,
    calls,
    submit: (options) => context.ui.submit(options),
    cancel: () => context.ui.cancelRequest(),
    rendered: () => rendered,
    lang: (value) => { lang = value; langListener(); },
    advanceMinute: () => { clock = '15:01'; },
    lastRequest: () => structuredClone(vm.runInContext('state.lastRequest', context)),
  };
}

const answer = () => ({ summary: 'Places nearby', cards: [], data_used: { generation: 'template' } });
const tick = () => new Promise((resolve) => setImmediate(resolve));
async function complete(h) {
  const pending = h.submit();
  h.calls.at(-1).resolve(answer());
  await pending;
}

test('editing query marks old cards stale; restoring the exact query clears the warning', async () => {
  const h = harness();
  await complete(h);
  h.el('query').input('Coffee with a power outlet');
  assert.equal(h.el('stale').hidden, false);
  h.el('query').input('  Walk near EXPO  ');
  assert.equal(h.el('stale').hidden, true);
});

test('filter changes are compared to the completed request, including toggling back', async () => {
  const h = harness();
  await complete(h);
  h.el('budget-chips').children[0].click();
  assert.equal(h.el('stale').hidden, false);
  h.el('budget-chips').children[0].click();
  assert.equal(h.el('stale').hidden, true);
});

test('cancelling an update restores the old cards with their stale warning', async () => {
  const h = harness();
  await complete(h);
  h.el('budget-chips').children[0].click();
  const pending = h.submit();
  h.cancel();
  await pending;
  assert.equal(h.rendered().kind, 'answer');
  assert.equal(h.el('stale').hidden, false);
});

test('parameters changed during the first request mark its eventual answer stale', async () => {
  const h = harness();
  h.el('budget-chips').children[1].click();
  const pending = h.submit();
  h.el('budget-chips').children[0].click();
  h.calls[0].resolve(answer());
  await pending;
  assert.equal(h.lastRequest().overrides.time_budget_min, 60);
  assert.equal(h.el('stale').hidden, false);
});

for (const [name, change] of [
  ['location', (h) => h.el('landmark').change('baiterek')],
  ['day', (h) => h.el('day-chips').children[1].click()],
  ['language', (h) => h.lang('en')],
  ['departure', (h) => { h.el('mode-chips').children[0].click(); h.el('departure-chips').children[2].click(); }],
]) {
  test(`${name} changes are included in the answer snapshot`, async () => {
    const h = harness();
    await complete(h);
    change(h);
    assert.equal(h.el('stale').hidden, false);
  });
}

test('language change during an update does not replace its loading/cancel UI with old cards', async () => {
  const h = harness();
  await complete(h);
  const pending = h.submit();
  h.lang('en');
  assert.equal(h.rendered().kind, 'loading');
  h.cancel();
  await pending;
  assert.equal(h.el('stale').hidden, false);
});

test('a timeout retries once without AI using the original request and accepts that answer', async () => {
  const h = harness();
  const pending = h.submit();
  h.calls[0].reject({ kind: 'timeout' });
  await tick();
  assert.equal(h.calls.length, 2);
  assert.deepEqual(h.calls[1].body, { ...h.calls[0].body, use_llm: false });
  assert.equal(h.el('status').textContent, 'status.retryNoAi');
  h.calls[1].resolve(answer());
  await pending;
  assert.equal(h.lastRequest().use_llm, false);
  assert.equal(h.rendered().kind, 'answer');
  assert.equal(h.el('stale').hidden, true);
});

test('a timeout does not retry a request whose conditions have changed', async () => {
  const h = harness();
  const pending = h.submit();
  h.el('query').input('Another request');
  h.calls[0].reject({ kind: 'timeout' });
  await pending;
  assert.equal(h.calls.length, 1);
  assert.equal(h.rendered().kind, 'error');
});

test('cancellation prevents a queued timeout from launching a retry', async () => {
  const h = harness();
  const pending = h.submit();
  h.calls[0].reject({ kind: 'timeout' });
  h.cancel();
  await pending;
  assert.equal(h.calls.length, 1);
  assert.equal(h.rendered().kind, 'intro');
});

test('cancelling the automatic fallback aborts it and restores the previous answer', async () => {
  const h = harness();
  await complete(h);
  const pending = h.submit();
  h.calls[1].reject({ kind: 'timeout' });
  await tick();
  assert.equal(h.calls.length, 3);
  h.cancel();
  await pending;
  assert.equal(h.calls[2].signal.aborted, true);
  assert.equal(h.rendered().kind, 'answer');
});

test('fallback timeout is surfaced without a retry loop', async () => {
  const h = harness();
  const pending = h.submit();
  h.calls[0].reject({ kind: 'timeout' });
  await tick();
  assert.equal(h.calls.length, 2);
  h.calls[1].reject({ kind: 'timeout' });
  await pending;
  assert.equal(h.calls.length, 2);
  assert.equal(h.rendered().kind, 'error');
  assert.equal(h.lastRequest().use_llm, false);
});

test('a new request supersedes an old timeout without retrying the old conditions', async () => {
  const h = harness();
  const first = h.submit();
  h.calls[0].reject({ kind: 'timeout' });
  h.el('query').input('New request');
  const second = h.submit();
  h.calls[1].resolve(answer());
  await Promise.all([first, second]);
  assert.equal(h.calls.length, 2);
  assert.equal(h.lastRequest().query, 'New request');
});

test('an explicit no-AI request does not automatically retry on timeout', async () => {
  const h = harness();
  const pending = h.submit({ useLlm: false });
  h.calls[0].reject({ kind: 'timeout' });
  await pending;
  assert.equal(h.calls.length, 1);
  assert.equal(h.rendered().kind, 'error');
});

test('conditions edited during the fallback mark its successful answer stale', async () => {
  const h = harness();
  const pending = h.submit();
  h.calls[0].reject({ kind: 'timeout' });
  await tick();
  h.el('budget-chips').children[0].click();
  h.calls[1].resolve(answer());
  await pending;
  assert.equal(h.el('stale').hidden, false);
});

test('tomorrow at the same time survives a clock tick and retries with the original timestamp', async () => {
  const h = harness();
  h.el('day-chips').children[1].click();
  const pending = h.submit();
  h.advanceMinute();
  h.calls[0].reject({ kind: 'timeout' });
  await tick();
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].body.at, h.calls[0].body.at);
  h.calls[1].resolve(answer());
  await pending;
  assert.equal(h.el('stale').hidden, true);
});
