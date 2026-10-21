// Карта пробелов данных (T143, контракт 1.4.0 — CityGaps в app/schemas.py, ARCHITECTURE §5): чистая логика
// без DOM — доли, пороги «мало данных», шкала и подписи с числами. Проверяется в node (web/tests/gaps.test.mjs).
// Доля = «нет данных» / «всего» и означает «в OSM нет сведений», а не «плохо»: это пробел в данных, не оценка района.

// Слои карты: поля GapCounts — «всего» и «нет данных»; unit — что считаем (места, метры дорожек, переходы)
export const GAP_LAYERS = [
  { id: 'hours', total: 'poi', part: 'poi_no_hours', unit: 'poi' },
  { id: 'wheelchair', total: 'poi', part: 'poi_wheelchair_unknown', unit: 'poi' },
  { id: 'name_kk', total: 'poi', part: 'poi_no_name_kk', unit: 'poi' },
  { id: 'lit', total: 'walk_m', part: 'walk_lit_unknown_m', unit: 'm' },
  { id: 'crossing_kind', total: 'crossings', part: 'crossings_kind_unknown', unit: 'crossings' },
  { id: 'kerb', total: 'crossings', part: 'crossings_kerb_unknown', unit: 'crossings' },
];

// = GAP_MIN_POI, GAP_MIN_WALK_M, GAP_MIN_CROSSINGS в app/schemas.py: ниже — «мало данных», без доли
export const GAP_MIN = { poi: 5, m: 300, crossings: 3 };

// Последовательная шкала доли «нет данных»: 5 классов, темнее — больше пробелов. Верхние границы классов.
export const GAP_BINS = [0.2, 0.4, 0.6, 0.8];

export const layerById = (id) => GAP_LAYERS.find((l) => l.id === id) || GAP_LAYERS[0];

export function layerCounts(counts, layer) {
  return { total: counts?.[layer.total] ?? 0, part: counts?.[layer.part] ?? 0 };
}

// 'none' — нечего считать (0 объектов), 'low' — меньше порога слоя («мало данных», серым), иначе класс 0–4
export function gapClass(counts, layer) {
  const { total, part } = layerCounts(counts, layer);
  if (!(total > 0)) return 'none';
  if (total < GAP_MIN[layer.unit]) return 'low';
  const share = Math.min(1, part / total);
  const i = GAP_BINS.findIndex((edge) => share < edge);
  return i === -1 ? GAP_BINS.length : i;
}

export const pct = (part, total) => Math.round((Math.min(part, total) / total) * 100);

// Подписи легенды: «0–20 %» … «80–100 %»
export function legendRanges() {
  const edges = [0, ...GAP_BINS, 1];
  return edges.slice(0, -1).map((lo, i) => ({ cls: i, from: Math.round(lo * 100), to: Math.round(edges[i + 1] * 100) }));
}

// ru: «из 21 места», «из 47 мест»; en: «1 place» / «47 places»; kk — без форм числа
function ruGenitive(n, one, many) {
  return n % 10 === 1 && n % 100 !== 11 ? one : many;
}

const NOUNS = {
  ru: { poi: ['места', 'мест'], crossings: ['перехода', 'переходов'] },
  en: { poi: ['place', 'places'], crossings: ['crossing', 'crossings'] },
  kk: { poi: ['орын', 'орын'], crossings: ['өткел', 'өткел'] },
};

export function noun(unit, n, lang) {
  const forms = (NOUNS[lang] || NOUNS.ru)[unit];
  if (!forms) return '';
  if (lang === 'ru') return ruGenitive(n, forms[0], forms[1]);
  return n === 1 ? forms[0] : forms[1];
}

// Числа с разрядами: ru и kk — «15 256», «4,7» (kk — по ru-RU: данные Intl для kk-KZ есть не везде), en — «15,256»
export function formatNumber(n, lang, digits = 0) {
  const locale = lang === 'en' ? 'en-GB' : 'ru-RU';
  return new Intl.NumberFormat(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n);
}

// Метры дорожек: до километра — «850 м», дальше — «4,7 км» (en — «4.7 km»)
export function formatLength(m, lang) {
  const ruLike = lang === 'kk' || lang === 'ru';
  if (m < 1000) return `${formatNumber(Math.round(m), lang)} ${ruLike ? 'м' : 'm'}`;
  return `${formatNumber(Math.round(m / 100) / 10, lang, 1)} ${ruLike ? 'км' : 'km'}`;
}

// Текст с числами для ячейки или города: ключ i18n + параметры (цвет не единственный носитель смысла)
//   gaps.text.<layer> — «у 31 из 47 мест нет часов работы (66 %)»
//   gaps.low.<unit>   — «мало данных: всего 3 места — долю не считаем»
//   gaps.none.<unit>  — «здесь нет мест»
export function gapText(counts, layer, lang) {
  const { total, part } = layerCounts(counts, layer);
  const cls = gapClass(counts, layer);
  const metres = layer.unit === 'm';
  const vars = {
    part: metres ? formatLength(part, lang) : formatNumber(part, lang),
    total: metres ? formatLength(total, lang) : formatNumber(total, lang),
    noun: metres ? '' : noun(layer.unit, total, lang),
    min: metres ? formatLength(GAP_MIN.m, lang) : GAP_MIN[layer.unit],
  };
  if (cls === 'none') return { key: `gaps.none.${layer.unit}`, vars };
  if (cls === 'low') return { key: `gaps.low.${layer.unit}`, vars };
  return { key: `gaps.text.${layer.id}`, vars: { ...vars, pct: pct(part, total) } };
}

// Минимальная проверка формы ответа (полную делает сервер по схеме): не рисуем то, что не похоже на CityGaps
export function isCityGaps(data) {
  return Boolean(
    data &&
      data.format_version === 1 &&
      data.h3_res === 8 &&
      data.city &&
      Array.isArray(data.cells) &&
      data.cells.every((c) => typeof c.h3_cell === 'string'),
  );
}

// Дата данных «2026-10-18» → «18.10.2026»
export function formatDate(isoDate) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(isoDate || ''));
  return m ? `${m[3]}.${m[2]}.${m[1]}` : null;
}
