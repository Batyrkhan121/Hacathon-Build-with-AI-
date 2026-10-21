// Карта пробелов данных (T143): чистая логика без DOM и без i18n — проверяется в node (web/tests/gaps.test.mjs).
// Данные: { built_at, resolution: 8, min_n, metrics, cells: [{ h3, district, metrics: { lit: { n, unknown } } }],
// districts: [{ id, name: { ru, kk, en }, metrics }] }. n — сколько объектов, к которым показатель относится
// (участки улиц, переходы, места), unknown — у скольких из них в OSM нет нужной метки.

export const GAP_METRICS = ['lit', 'kerb', 'crossing', 'access', 'hours'];

// Одна шкала оттенков: 5 классов по доле «нет данных», темнее — больше пробелов. Верхние границы классов.
export const GAP_BINS = [0.2, 0.4, 0.6, 0.8];

export function gapShare(v) {
  if (!v || !(v.n > 0)) return null;
  return Math.min(1, Math.max(0, v.unknown / v.n));
}

// 'none' — объектов нет (ячейку не красим), 'low' — меньше min_n объектов («мало данных», штриховка),
// иначе номер класса 0–4
export function gapClass(v, minN) {
  if (!v || !(v.n > 0)) return 'none';
  if (v.n < minN) return 'low';
  const share = gapShare(v);
  const i = GAP_BINS.findIndex((edge) => share < edge);
  return i === -1 ? GAP_BINS.length : i;
}

export const pct = (share) => Math.round(share * 100);

// Подписи легенды: «0–20 %», «20–40 %», … «80–100 %»
export function legendRanges() {
  const edges = [0, ...GAP_BINS, 1];
  return edges.slice(0, -1).map((lo, i) => ({ cls: i, from: pct(lo), to: pct(edges[i + 1]) }));
}

// Строки таблицы по районам для показателя: районы по алфавиту языка, итог по городу последней строкой
export function districtRows(data, metric, lang, minN = data?.min_n ?? 5) {
  const rows = (data?.districts || []).map((d) => {
    const v = d.metrics?.[metric] || { n: 0, unknown: 0 };
    return {
      id: d.id,
      name: d.name?.[lang] || d.name?.ru || d.id,
      n: v.n,
      unknown: v.unknown,
      share: gapShare(v),
      cls: gapClass(v, minN),
    };
  });
  rows.sort((a, b) => a.name.localeCompare(b.name, lang));
  const total = rows.reduce((s, r) => ({ n: s.n + r.n, unknown: s.unknown + r.unknown }), { n: 0, unknown: 0 });
  rows.push({ id: 'total', name: null, ...total, share: gapShare(total), cls: gapClass(total, minN), total: true });
  return rows;
}

// Текст по нажатию на шестиугольник: ключ i18n + параметры (цифры текстом, а не только цветом)
export function cellSummary(cell, metric, minN) {
  const v = cell?.metrics?.[metric];
  const cls = gapClass(v, minN);
  if (cls === 'none') return { key: 'gaps.cellNone', vars: {} };
  const vars = { unknown: v.unknown, n: v.n, pct: pct(gapShare(v)) };
  return { key: cls === 'low' ? 'gaps.cellLow' : 'gaps.cell', vars };
}

// Минимальная проверка формы: на странице не рисуем то, что не похоже на данные пробелов
export function isGapsData(data) {
  return Boolean(
    data &&
      Array.isArray(data.cells) &&
      Array.isArray(data.districts) &&
      data.cells.every((c) => typeof c.h3 === 'string' && c.metrics && typeof c.metrics === 'object'),
  );
}

// Дата сборки по Астане (UTC+5): «21.10.2026»
export function buildDate(iso) {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  const d = new Date(ms + 5 * 3600 * 1000);
  const pad = (x) => String(x).padStart(2, '0');
  return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)}.${d.getUTCFullYear()}`;
}
