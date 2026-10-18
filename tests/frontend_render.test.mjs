// Run: node --test tests/frontend_render.test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

class Element {
  constructor(tag = 'text') {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.ownText = '';
  }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map((c) => c.textContent).join(''); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() {}
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ''; this.children = children; }
}

function byClass(node, name) {
  return [
    ...(node.className?.split(' ').includes(name) ? [node] : []),
    ...node.children.flatMap((child) => byClass(child, name)),
  ];
}

function renderer() {
  const context = vm.createContext({
    Node: Element,
    document: {
      createElement: (tag) => new Element(tag),
      createTextNode: (text) => Object.assign(new Element(), { textContent: text }),
      querySelectorAll: () => [],
      documentElement: new Element('html'),
    },
    location: { search: '' },
    navigator: { language: 'ru' },
    localStorage: { getItem: () => null, setItem() {} },
    URLSearchParams,
    URL,
    dayOffsetOf: () => 0,
  });
  for (const name of ['i18n.js', 'render.js']) {
    const source = readFileSync(new URL(`../web/${name}`, import.meta.url), 'utf8')
      .replace(/^import[^\n]+\n/gm, '').replace(/^export /gm, '');
    vm.runInContext(source, context);
  }
  vm.runInContext('globalThis.ui = { renderAnswer, setLang };', context);
  return context.ui;
}

const sample = JSON.parse(readFileSync(new URL('../web/mock/answer_sample.json', import.meta.url), 'utf8'));

for (const [lang, planned, range] of [
  ['ru', 'на месте 20 мин', 'на месте 20–49 мин'],
  ['kk', 'орнында 20 мин', 'орнында 20–49 мин'],
  ['en', '20 min there', '20–49 min there'],
]) {
  test(`route timeline shows the planned visit used in total_min; card keeps its range (${lang})`, () => {
    const ui = renderer();
    ui.setLang(lang);
    const card = { ...sample.cards[0], visit_min_low: 20, visit_min_high: 49, walk_min: 11 };
    const response = {
      ...sample,
      lang,
      cards: [card],
      route: {
        ...sample.route,
        total_min: 42,
        stop_ids: [card.poi_id],
        legs: [
          { from_id: 'origin', to_id: card.poi_id, distance_m: 600, walk_min: 11 },
          { from_id: card.poi_id, to_id: 'origin', distance_m: 600, walk_min: 11 },
        ],
      },
    };
    const root = new Element('div');
    ui.renderAnswer(root, response, {});
    assert.equal(byClass(root, 'tl-visit')[0].textContent, ` · ${planned}`);
    assert.ok(byClass(root, 'card-facts')[0].textContent.includes(range));
    assert.ok(byClass(root, 'route-total')[0].textContent.includes('42'));
  });
}
