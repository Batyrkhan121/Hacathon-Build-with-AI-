// Карта пробелов данных (T143, контракт 1.4.0): доли, пороги «мало данных», шкала и подписи с числами, ru/kk/en.
// Запуск: node --test "web/tests/*.test.mjs"
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  GAP_BINS,
  GAP_LAYERS,
  GAP_MIN,
  attributionUrl,
  formatBuiltAt,
  formatDate,
  formatLength,
  formatNumber,
  gapClass,
  gapText,
  isCityGaps,
  layerById,
  legendRanges,
  noun,
} from '../gaps-core.js';
import { setLang, t } from '../i18n.js';

const read = (rel) => readFileSync(new URL(rel, import.meta.url), 'utf8');
const mock = JSON.parse(read('../mock/city_gaps.json'));
const large = JSON.parse(read('../mock/city_gaps_large.json'));
const schemas = read('../../app/schemas.py');
const L = (id) => layerById(id);
const say = (lang, s) => {
  setLang(lang);
  return t(s.key, s.vars);
};

test('слои — поля GapCounts из контракта 1.4.0', () => {
  assert.deepEqual(
    GAP_LAYERS.map((l) => [l.id, l.part, l.total]),
    [
      ['hours', 'poi_no_hours', 'poi'],
      ['wheelchair', 'poi_wheelchair_unknown', 'poi'],
      ['name_kk', 'poi_no_name_kk', 'poi'],
      ['lit', 'walk_lit_unknown_m', 'walk_m'],
      ['crossing_kind', 'crossings_kind_unknown', 'crossings'],
      ['kerb', 'crossings_kerb_unknown', 'crossings'],
    ],
  );
  for (const l of GAP_LAYERS) {
    assert.ok(schemas.includes(`    ${l.part}: int`), l.part);
    assert.ok(schemas.includes(`    ${l.total}: int`), l.total);
  }
});

test('пороги «мало данных» совпадают с GAP_MIN_* в app/schemas.py', () => {
  const val = (name) => Number(new RegExp(`^${name} = (\\d+)`, 'm').exec(schemas)[1]);
  assert.deepEqual(GAP_MIN, { poi: val('GAP_MIN_POI'), m: val('GAP_MIN_WALK_M'), crossings: val('GAP_MIN_CROSSINGS') });
});

test('ниже порога — «мало данных», с порога — доля; пусто — «нечего считать»', () => {
  assert.equal(gapClass({ poi: 4, poi_no_hours: 4 }, L('hours')), 'low');
  assert.equal(gapClass({ poi: 5, poi_no_hours: 5 }, L('hours')), 4);
  assert.equal(gapClass({ walk_m: 299, walk_lit_unknown_m: 0 }, L('lit')), 'low');
  assert.equal(gapClass({ walk_m: 300, walk_lit_unknown_m: 0 }, L('lit')), 0);
  assert.equal(gapClass({ crossings: 2, crossings_kerb_unknown: 1 }, L('kerb')), 'low');
  assert.equal(gapClass({ crossings: 3, crossings_kerb_unknown: 1 }, L('kerb')), 1);
  assert.equal(gapClass({ poi: 0, walk_m: 500 }, L('hours')), 'none');
});

test('шкала: 5 классов по доле, темнее — больше пробелов; легенда с числами', () => {
  const cls = (part) => gapClass({ poi: 100, poi_no_hours: part }, L('hours'));
  assert.deepEqual([0, 19, 20, 39, 40, 59, 60, 79, 80, 100].map(cls), [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]);
  assert.deepEqual(GAP_BINS, [0.2, 0.4, 0.6, 0.8]);
  assert.deepEqual(legendRanges().map((r) => `${r.from}–${r.to}`), ['0–20', '20–40', '40–60', '60–80', '80–100']);
});

test('нажатие на ячейку: «у 31 из 47 мест нет часов работы» на ru/kk/en', () => {
  const c = { poi: 47, poi_no_hours: 31 };
  assert.equal(say('ru', gapText(c, L('hours'), 'ru')), 'у 31 из 47 мест нет часов работы (66 %)');
  assert.equal(say('kk', gapText(c, L('hours'), 'kk')), '31 / 47 орын: жұмыс уақыты жоқ (66 %)');
  assert.equal(say('en', gapText(c, L('hours'), 'en')), '31 of 47 places have no opening hours (66 %)');
});

test('ru: «из 21 места», «из 11 мест»; переходы; метры — в км', () => {
  assert.equal(say('ru', gapText({ poi: 21, poi_no_name_kk: 5 }, L('name_kk'), 'ru')), 'у 5 из 21 места нет названия на казахском (24 %)');
  assert.equal(noun('poi', 11, 'ru'), 'мест');
  assert.equal(noun('crossings', 31, 'ru'), 'перехода');
  assert.equal(say('ru', gapText({ crossings: 14, crossings_kerb_unknown: 13 }, L('kerb'), 'ru')), 'у 13 из 14 переходов не отмечен бордюр (93 %)');
  assert.equal(say('ru', gapText({ walk_m: 5923, walk_lit_unknown_m: 4747 }, L('lit'), 'ru')), 'без сведений об освещении — 4,7 км из 5,9 км дорожек (80 %)');
  assert.equal(say('en', gapText({ walk_m: 5923, walk_lit_unknown_m: 4747 }, L('lit'), 'en')), 'no lighting data for 4.7 km of 5.9 km of footpaths (80 %)');
  assert.deepEqual([formatLength(850, 'ru'), formatLength(4747, 'kk'), formatLength(1000, 'en')], ['850 м', '4,7 км', '1.0 km']);
  // разряды: ru и kk — неразрывный пробел (ru-RU), en — запятая
  assert.equal(formatLength(3348300, 'ru'), '3 348,3 км');
  assert.deepEqual([formatNumber(15256, 'kk'), formatNumber(15256, 'en')], ['15 256', '15,256']);
  assert.equal(say('ru', gapText({ poi: 15256, poi_no_hours: 7524 }, L('hours'), 'ru')), 'у 7 524 из 15 256 мест нет часов работы (49 %)');
});

test('«мало данных» и «нечего считать» — текстом, без доли, ru/kk/en', () => {
  assert.equal(say('ru', gapText({ poi: 3, poi_no_hours: 1 }, L('hours'), 'ru')), 'мало данных: мест меньше 5 — долю не считаем');
  assert.equal(say('kk', gapText({ walk_m: 120, walk_lit_unknown_m: 120 }, L('lit'), 'kk')), 'дерек аз (жол < 300 м) — үлесті есептемейміз');
  assert.equal(say('en', gapText({ crossings: 2, crossings_kind_unknown: 2 }, L('crossing_kind'), 'en')), 'too little data: under 3 crossings — no share');
  assert.equal(say('ru', gapText({ poi: 0, walk_m: 900 }, L('wheelchair'), 'ru')), 'здесь нет мест из нашей базы');
});

test('атрибуция и строка смысла на ru/kk/en; слов «плохо», «опасно» нет', () => {
  const want = { ru: '© участники OpenStreetMap', kk: '© OpenStreetMap қатысушылары', en: '© OpenStreetMap contributors' };
  const banned = /плох|опасн|жаман|қауіп|\bbad\b|danger/i;
  const gapsLines = read('../i18n.js').split('\n').filter((line) => line.includes("'gaps."));
  assert.ok(gapsLines.length > 150);
  for (const line of gapsLines) assert.doesNotMatch(line, banned, line);
  for (const [lang, attr] of Object.entries(want)) {
    setLang(lang);
    assert.equal(t('gaps.attrOsm'), attr);
    assert.notEqual(t('gaps.meaning'), 'gaps.meaning');
    assert.match(t('gaps.attrLicense'), /ODbL/);
    for (const l of GAP_LAYERS) assert.notEqual(t(`gaps.layer.${l.id}`), `gaps.layer.${l.id}`);
  }
});

test('даты данных: «2026-10-18» → «18.10.2026»', () => {
  assert.equal(formatDate('2026-10-18'), '18.10.2026');
  assert.equal(formatDate('18.10.2026'), null);
});

test('моки — CityGaps: итог города = сумма по ячейкам; большой — больше тысячи ячеек r8', () => {
  for (const data of [mock, large]) {
    assert.equal(isCityGaps(data), true);
    for (const k of Object.keys(data.city)) {
      assert.equal(data.cells.reduce((s, c) => s + c[k], 0), data.city[k], k);
    }
  }
  assert.ok(large.cells.length > 1000 && large.cells.length <= 5000, String(large.cells.length));
  assert.equal(isCityGaps({ format_version: 1, h3_res: 9, city: {}, cells: [] }), false);
});

test('дата сборки built_at — по Астане (UTC+5)', () => {
  assert.equal(formatBuiltAt('2026-10-22T09:00:00+05:00'), '22.10.2026');
  assert.equal(formatBuiltAt('2026-10-21T20:30:00Z'), '22.10.2026');
  assert.equal(formatBuiltAt('нет'), null);
});

test('атрибуция — ссылки из полей файла, но только https и свои хосты', () => {
  assert.equal(attributionUrl('copyright', mock.copyright_url), 'https://www.openstreetmap.org/copyright');
  assert.equal(attributionUrl('license', mock.license_url), 'https://opendatacommons.org/licenses/odbl/1-0/');
  assert.equal(attributionUrl('copyright', 'http://www.openstreetmap.org/copyright'), 'https://www.openstreetmap.org/copyright');
  assert.equal(attributionUrl('license', 'https://example.org/odbl'), 'https://opendatacommons.org/licenses/odbl/1-0/');
  assert.equal(attributionUrl('copyright', 'javascript:alert(1)'), 'https://www.openstreetmap.org/copyright');
});

test('внешние ссылки страницы — только по клику, noopener noreferrer; картинок и скриптов извне нет', () => {
  const html = read('../gaps.html');
  const hrefs = [...html.matchAll(/<a [^>]*href="(https:[^"]+)"[^>]*>/g)].map((m) => [m[1], m[0]]);
  assert.deepEqual(hrefs.map(([h]) => h).sort(), [
    'https://opendatacommons.org/licenses/odbl/1-0/',
    'https://streetcomplete.app/',
    'https://www.openstreetmap.org/',
    'https://www.openstreetmap.org/copyright',
  ]);
  for (const [, tag] of hrefs) assert.match(tag, /rel="noopener noreferrer"/);
  assert.doesNotMatch(html, /<(script|img|link)[^>]+(src|href)="https?:/);
  for (const lang of ['ru', 'kk', 'en']) {
    setLang(lang);
    assert.notEqual(t('gaps.linkStreetComplete'), 'gaps.linkStreetComplete');
    assert.notEqual(t('gaps.linkOsmEdit'), 'gaps.linkOsmEdit');
    assert.match(t('gaps.dates', { built: '1', poi: '2', walk: '3' }), /1.*2.*3/);
  }
});
