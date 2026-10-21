// «Без кафе» и категория «кафе» (вариант B, решение Megah 21.10). Запуск: node --test "web/tests/*.test.mjs"
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { setLang, t } from '../i18n.js';
import {
  cafeAvoidedNote,
  cafeOnlyQuery,
  mergePatch,
  mergePrefs,
  noCafeChipState,
  offerDropNoCafe,
} from '../refine.js';

const ans = (categories, cards = [{}]) => ({ intent: { categories }, cards });
const req = (avoids = [], overrides = null) => ({ prefs: avoids.length ? { likes: [], avoids } : null, overrides });

// ---- случай 1: «Без кафе» неактивен только когда ищут именно кафе

test('запрос про одни кафе («хочу кофе») — чип «Без кафе» неактивен', () => {
  assert.equal(cafeOnlyQuery(ans(['cafe']), req()), true);
  assert.equal(noCafeChipState(ans(['cafe']), req()), 'disabled');
});

test('выбранная категория «кафе» (overrides.categories) — неактивен, даже среди других', () => {
  assert.equal(noCafeChipState(ans(['museum']), req([], { categories: ['cafe'] })), 'disabled');
  assert.equal(noCafeChipState(ans([]), req([], { categories: ['cafe', 'restaurant'] })), 'disabled');
  assert.equal(noCafeChipState(ans(['cafe']), req([], { categories: ['museum'] })), 'active');
});

test('смешанный запрос о еде («где поесть»: кафе, ресторан, фастфуд) — «Без кафе» активен', () => {
  assert.equal(noCafeChipState(ans(['cafe', 'restaurant', 'fast_food']), req()), 'active');
});

test('без категорий в разборе — чип активен; «без кафе» уже действует — скрыт', () => {
  assert.equal(noCafeChipState(ans([]), req()), 'active');
  assert.equal(noCafeChipState({}, req()), 'active');
  assert.equal(noCafeChipState(ans(['cafe']), req(['cafes'])), 'hidden');
});

test('строка «почему нет кафе» — только при «без кафе» и запросе про одни кафе', () => {
  assert.equal(cafeAvoidedNote(ans(['cafe']), req(['cafes'])), true);
  assert.equal(cafeAvoidedNote(ans(['cafe', 'restaurant']), req(['cafes'])), false);
  assert.equal(cafeAvoidedNote(ans(['cafe']), req(['bars'])), false);
});

test('подсказка «Вы ищете кафе — этот фильтр здесь не подходит» на ru/kk/en', () => {
  const want = {
    ru: 'Вы ищете кафе — этот фильтр здесь не подходит.',
    kk: 'Сіз кафе іздеп жатырсыз — бұл сүзгі мұнда сай келмейді.',
    en: 'You’re looking for cafés — this filter doesn’t fit here.',
  };
  for (const [lang, text] of Object.entries(want)) {
    setLang(lang);
    assert.equal(t('noCafe.queryIsCafe'), text);
  }
});

// ---- случай 2: после «Без кафе» мест не осталось — пустой ответ с кнопкой «Убрать «Без кафе»»

test('пустой ответ при «без кафе» — предлагаем убрать; с местами или без «без кафе» — нет', () => {
  assert.equal(offerDropNoCafe(ans(['cafe'], []), req(['cafes'])), true);
  assert.equal(offerDropNoCafe(ans([], []), req(['bars', 'cafes'])), true);
  assert.equal(offerDropNoCafe(ans(['cafe'], [{}]), req(['cafes'])), false);
  assert.equal(offerDropNoCafe(ans(['cafe'], []), req(['bars'])), false);
});

test('«Убрать «Без кафе»» снимает cafes и из чипа, и из «Моих предпочтений» — только в этом запросе', () => {
  const saved = { likes: ['museums'], avoids: ['cafes', 'bars'] };
  // «Без кафе» чипом, потом «Убрать»
  const p1 = mergePatch(mergePatch({}, { avoids: ['cafes'] }), { drop: ['cafes'] });
  assert.deepEqual(mergePrefs(saved, p1), { likes: ['museums'], avoids: ['bars'] });
  assert.deepEqual(saved, { likes: ['museums'], avoids: ['cafes', 'bars'] }); // сохранённое не меняется
  // снова «Без кафе» после «Убрать» — метка возвращается
  const p2 = mergePatch(p1, { avoids: ['cafes'] });
  assert.deepEqual(mergePrefs(saved, p2).avoids, ['cafes', 'bars']);
  // «Убрать» не трогает другие поправки серии
  const p3 = mergePatch(mergePatch({}, { overrides: { max_walk_min: 10 } }), { drop: ['cafes'] });
  assert.deepEqual(p3.overrides, { max_walk_min: 10 });
});

test('кнопка и подсказка пустого ответа на ru/kk/en называют чип так же, как он подписан', () => {
  for (const lang of ['ru', 'kk', 'en']) {
    setLang(lang);
    const chip = t('refineChip.noCafe');
    assert.ok(t('empty.dropNoCafe').includes(chip), `${lang}: ${t('empty.dropNoCafe')}`);
    assert.ok(t('empty.noCafeHint').includes(chip), `${lang}: ${t('empty.noCafeHint')}`);
  }
});

test('пустой ответ: одна инструкция — кнопка «Убрать», без строки «снимите в «Не хочу»»', () => {
  assert.equal(cafeAvoidedNote(ans(['cafe'], []), req(['cafes'])), false);
  assert.equal(offerDropNoCafe(ans(['cafe'], []), req(['cafes'])), true);
  assert.equal(cafeAvoidedNote(ans(['cafe'], [{}]), req(['cafes'])), true);
});

// ---- ревью f8a2d10: возвращённые проверки и серия поправок

test('строка «почему нет кафе» без «Моих предпочтений» — нет', () => {
  assert.equal(cafeAvoidedNote(ans(['cafe']), req()), false);
});

test('«без кафе» среди других avoids (bars, cafes) — чип скрыт', () => {
  assert.equal(noCafeChipState(ans(['park']), req(['bars', 'cafes'])), 'hidden');
});

test('строка «почему нет кафе» называет метку и группу так же, как они подписаны, ru/kk/en', () => {
  for (const lang of ['ru', 'kk', 'en']) {
    setLang(lang);
    const note = t('noCafe.avoidedForQuery');
    assert.ok(note.includes(t('avoid.cafes')) && note.includes(t('prefs.avoids')), `${lang}: ${note}`);
  }
});

test('«Убрать «Без кафе»» только из сохранённых предпочтений (без чипа) — cafes уходит в этом запросе', () => {
  const saved = { likes: ['coffee'], avoids: ['cafes', 'noisy'] }; // как после ручной правки хранилища
  const p = mergePatch({}, { drop: ['cafes'] });
  assert.deepEqual(p.avoids, []);
  assert.deepEqual(mergePrefs(saved, p), { likes: ['coffee'], avoids: ['noisy'] });
  assert.deepEqual(saved.avoids, ['cafes', 'noisy']);
});

test('серия «Ближе» → «Без кафе» → «Ещё» → «Убрать «Без кафе»»: поправки складываются, drop снимает только cafes', () => {
  const saved = { likes: ['coffee', 'parks'], avoids: ['bars'] };
  let p = mergePatch({}, { overrides: { max_walk_min: 12 } });
  p = mergePatch(p, { avoids: ['cafes'] });
  assert.deepEqual(mergePrefs(saved, p), { likes: ['parks'], avoids: ['bars', 'cafes'] }); // «без кафе» снимает «кофе»
  p = mergePatch(p, { exclude: ['osm:node/1', 'osm:node/2'] });
  p = mergePatch(p, { drop: ['cafes'] });
  assert.deepEqual(p.overrides, { max_walk_min: 12 });
  assert.deepEqual(p.exclude, ['osm:node/1', 'osm:node/2']);
  assert.deepEqual(p.avoids, []);
  assert.deepEqual(p.drop, ['cafes']);
  // «Без кафе» убран — сохранённое «кофе» снова действует (его снимал только «Без кафе»)
  assert.deepEqual(mergePrefs(saved, p), { likes: ['coffee', 'parks'], avoids: ['bars'] });
});
