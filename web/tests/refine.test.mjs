// «Без кафе» и категория «кафе» в запросе (вариант B). Запуск: node --test "web/tests/*.test.mjs"
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { setLang, t } from '../i18n.js';
import { cafeAvoidedNote, cafeOnlyQuery, noCafeChipState } from '../refine.js';

const ans = (categories) => ({ intent: { categories } });
const req = (avoids = [], overrides = null) => ({ prefs: avoids.length ? { likes: [], avoids } : null, overrides });

test('запрос про одни кафе («хочу кофе») — чип «Без кафе» неактивен', () => {
  assert.equal(cafeOnlyQuery(ans(['cafe']), req()), true);
  assert.equal(noCafeChipState(ans(['cafe']), req()), 'disabled');
});

test('«где поесть» (кафе, ресторан, фастфуд) — «Без кафе» остаётся уточнением', () => {
  assert.equal(noCafeChipState(ans(['cafe', 'restaurant', 'fast_food']), req()), 'active');
});

test('без категорий в разборе — чип активен', () => {
  assert.equal(noCafeChipState(ans([]), req()), 'active');
  assert.equal(noCafeChipState({}, req()), 'active');
});

test('«без кафе» уже действует (предпочтения или чип) — чип скрыт', () => {
  assert.equal(noCafeChipState(ans(['cafe']), req(['cafes'])), 'hidden');
  assert.equal(noCafeChipState(ans(['park']), req(['bars', 'cafes'])), 'hidden');
});

test('выбранная категория «кафе» (overrides.categories) — неактивен, даже среди других', () => {
  assert.equal(noCafeChipState(ans(['museum']), req([], { categories: ['cafe'] })), 'disabled');
  assert.equal(noCafeChipState(ans([]), req([], { categories: ['cafe', 'restaurant'] })), 'disabled');
  assert.equal(noCafeChipState(ans(['cafe']), req([], { categories: ['museum'] })), 'active');
});

test('строка «почему нет кафе» — только при «без кафе» и запросе про одни кафе', () => {
  assert.equal(cafeAvoidedNote(ans(['cafe']), req(['cafes'])), true);
  assert.equal(cafeAvoidedNote(ans(['cafe', 'restaurant']), req(['cafes'])), false);
  assert.equal(cafeAvoidedNote(ans(['cafe']), req(['bars'])), false);
  assert.equal(cafeAvoidedNote(ans(['cafe']), req()), false);
});

test('подсказки есть на ru/kk/en и называют чип так же, как он подписан', () => {
  for (const lang of ['ru', 'kk', 'en']) {
    setLang(lang);
    const want = {
      ru: 'Вы ищете кафе — этот фильтр здесь не подходит.',
      kk: 'Сіз кафе іздеп жатырсыз — бұл сүзгі мұнда сай келмейді.',
      en: 'You’re looking for cafés — this filter doesn’t fit here.',
    };
    assert.equal(t('noCafe.queryIsCafe'), want[lang]);
    const note = t('noCafe.avoidedForQuery');
    assert.ok(note.includes(t('avoid.cafes')) && note.includes(t('prefs.avoids')), `${lang}: ${note}`);
  }
});
