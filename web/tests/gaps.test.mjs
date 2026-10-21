// Карта пробелов данных (T143): шкала, «мало данных», таблица по районам, текст по нажатию, ru/kk/en.
// Запуск: node --test "web/tests/*.test.mjs"
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  GAP_METRICS,
  buildDate,
  cellSummary,
  districtRows,
  gapClass,
  gapShare,
  isGapsData,
  legendRanges,
} from '../gaps-core.js';
import { setLang, t } from '../i18n.js';

const mock = JSON.parse(readFileSync(new URL('../mock/gaps_sample.json', import.meta.url), 'utf8'));

test('доля «нет данных» и класс шкалы: 5 классов, темнее — больше пробелов', () => {
  assert.equal(gapShare({ n: 120, unknown: 84 }), 0.7);
  assert.equal(gapClass({ n: 100, unknown: 0 }, 5), 0);
  assert.equal(gapClass({ n: 100, unknown: 19 }, 5), 0);
  assert.equal(gapClass({ n: 100, unknown: 20 }, 5), 1);
  assert.equal(gapClass({ n: 100, unknown: 59 }, 5), 2);
  assert.equal(gapClass({ n: 100, unknown: 79 }, 5), 3);
  assert.equal(gapClass({ n: 100, unknown: 80 }, 5), 4);
  assert.equal(gapClass({ n: 100, unknown: 100 }, 5), 4);
});

test('мало объектов — «мало данных» (штриховка), нет объектов — «none», а не 0 %', () => {
  assert.equal(gapClass({ n: 4, unknown: 4 }, 5), 'low');
  assert.equal(gapClass({ n: 5, unknown: 5 }, 5), 4);
  assert.equal(gapClass({ n: 0, unknown: 0 }, 5), 'none');
  assert.equal(gapClass(undefined, 5), 'none');
  assert.equal(gapShare({ n: 0, unknown: 0 }), null);
});

test('легенда с числами: 0–20 … 80–100 %', () => {
  assert.deepEqual(
    legendRanges().map((r) => `${r.from}–${r.to}`),
    ['0–20', '20–40', '40–60', '60–80', '80–100'],
  );
});

test('таблица по районам: те же цифры, итог — сумма районов, порядок по алфавиту языка', () => {
  const data = {
    min_n: 5,
    districts: [
      { id: 'yesil', name: { ru: 'Есиль', kk: 'Есіл', en: 'Yesil' }, metrics: { lit: { n: 100, unknown: 70 } } },
      { id: 'almaty', name: { ru: 'Алматы', kk: 'Алматы', en: 'Almaty' }, metrics: { lit: { n: 3, unknown: 1 } } },
      { id: 'nura', name: { ru: 'Нура', kk: 'Нұра', en: 'Nura' }, metrics: {} },
    ],
  };
  const rows = districtRows(data, 'lit', 'ru');
  assert.deepEqual(rows.map((r) => r.name), ['Алматы', 'Есиль', 'Нура', null]);
  assert.deepEqual(rows.map((r) => r.cls), ['low', 3, 'none', 3]);
  const total = rows.at(-1);
  assert.equal(total.total, true);
  assert.deepEqual([total.unknown, total.n], [71, 103]);
  assert.deepEqual(districtRows(data, 'lit', 'kk').map((r) => r.name).slice(0, 3), ['Алматы', 'Есіл', 'Нұра']);
});

test('нажатие на шестиугольник — цифры текстом на ru/kk/en', () => {
  const cell = { metrics: { lit: { n: 120, unknown: 84 }, kerb: { n: 3, unknown: 2 } } };
  const want = {
    ru: ['нет данных у 84 из 120 (70 %)', 'мало данных — всего 3, без отметки 2, долю не считаем', 'здесь таких объектов нет'],
    kk: ['дерек жоқ — 84 / 120 (70 %)', 'дерек аз — барлығы 3, белгісізі 2, үлесті есептемейміз', 'мұнда мұндай нысандар жоқ'],
    en: ['no data for 84 of 120 (70 %)', 'too little data — 3 in total, 2 untagged, no share', 'no such objects here'],
  };
  for (const [lang, texts] of Object.entries(want)) {
    setLang(lang);
    const say = (m) => {
      const s = cellSummary(cell, m, 5);
      return t(s.key, s.vars);
    };
    assert.deepEqual([say('lit'), say('kerb'), say('hours')], texts);
  }
});

test('все подписи страницы есть на ru/kk/en (без пропущенных ключей)', () => {
  const keys = [
    'gaps.title', 'gaps.lead', 'gaps.link', 'gaps.helpPeople', 'gaps.helpCity', 'gaps.built', 'gaps.attribution',
    'gaps.legendLow', 'gaps.tableCaption', 'gaps.total', 'gaps.lowShort', 'gaps.emptyTitle',
    ...GAP_METRICS.flatMap((m) => [`gaps.metric.${m}`, `gaps.counted.${m}`]),
  ];
  for (const lang of ['ru', 'kk', 'en']) {
    setLang(lang);
    for (const k of keys) assert.notEqual(t(k), k, `${lang}: нет ${k}`);
    assert.ok(t('gaps.helpPeople').includes('StreetComplete'), lang);
    assert.ok(t('gaps.attribution').includes('OpenStreetMap'), lang);
  }
});

test('дата сборки — по Астане (UTC+5)', () => {
  assert.equal(buildDate('2026-10-21T06:00:00+05:00'), '21.10.2026');
  assert.equal(buildDate('2026-10-20T20:00:00Z'), '21.10.2026');
  assert.equal(buildDate('не дата'), null);
});

test('мок проходит проверку формы, помечен как мок и сходится с районами', () => {
  assert.equal(isGapsData(mock), true);
  assert.equal(mock.mock, true);
  assert.equal(isGapsData({ cells: 'x' }), false);
  for (const m of GAP_METRICS) {
    const fromCells = mock.cells.reduce((s, c) => s + (c.metrics[m]?.n || 0), 0);
    const fromDistricts = mock.districts.reduce((s, d) => s + (d.metrics[m]?.n || 0), 0);
    assert.equal(fromCells, fromDistricts, m);
  }
});
