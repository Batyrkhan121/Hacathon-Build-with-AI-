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
  // класс — по той же округлённой доле, что в подписи: 79,6 % подписано «80 %» и закрашено как 80–100 %
  const p = pct(part, total);
  const i = GAP_BINS.findIndex((edge) => p < Math.round(edge * 100));
  return i === -1 ? GAP_BINS.length : i;
}

// Доля в процентах, округлённая один раз: её берут и подпись, и класс шкалы
export function pct(part, total) {
  return Math.round((Math.min(part, total) / total) * 100);
}

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
      data.cells.every((c) => c && typeof c.h3_cell === 'string'),
  );
}

// Дата данных «2026-10-18» → «18.10.2026»
export function formatDate(isoDate) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(isoDate || ''));
  return m ? `${m[3]}.${m[2]}.${m[1]}` : null;
}

// Время сборки (built_at, с поясом) → дата по Астане (UTC+5): «2026-10-22T09:00:00+05:00» → «22.10.2026»
export function formatBuiltAt(iso) {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  return formatDate(new Date(ms + 5 * 3600 * 1000).toISOString().slice(0, 10));
}

// Ссылки атрибуции — из полей файла (copyright_url, license_url), но только https и только свои хосты:
// иначе — адрес из контракта 1.4.0 (страница не ведёт туда, куда не обещала)
const ATTR_HOSTS = { copyright: 'www.openstreetmap.org', license: 'opendatacommons.org' };
export const ATTR_DEFAULTS = {
  copyright: 'https://www.openstreetmap.org/copyright',
  license: 'https://opendatacommons.org/licenses/odbl/1-0/',
};

export function attributionUrl(kind, url) {
  try {
    const u = new URL(url);
    if (u.protocol === 'https:' && u.hostname === ATTR_HOSTS[kind]) return u.href;
  } catch {
    // не адрес — берём из контракта
  }
  return ATTR_DEFAULTS[kind];
}

// ------------------------------------------------------------------ районы (контракт 1.4.1, CityGaps.districts)
// Блок необязателен: без него таблицы районов нет. Район — те же шесть пар «всего / нет данных», без оценок.
// status: ok — граница из OSM; stale — граница в OSM устарела (считаем по ней и пишем об этом);
// missing — границы в OSM нет: ни контура, ни чисел, его территория — в unassigned («вне границ районов»).

export const hasDistricts = (data) => Array.isArray(data?.districts?.items) && data.districts.items.length > 0;

// Список районов сверен с сайтом акимата? Если нет — интерфейс пишет «список районов не сверен»
export const districtListChecked = (data) =>
  Boolean(data?.districts?.official_list_url && data?.districts?.official_list_checked);

export const DISTRICT_SORTS = ['name', 'share-desc', 'share-asc'];

// Строки таблицы районов для слоя. sort: 'name' — по алфавиту языка страницы (localeCompare),
// 'share-desc' / 'share-asc' — по доле «нет данных»; строки без доли («мало данных», «нечего считать», missing)
// при сортировке по доле идут после строк с долей, по алфавиту. «Вне границ районов» — всегда последней,
// и только если там есть ячейки. Пороги «мало данных» — те же, что у ячеек (gapClass).
export function districtRows(data, layer, lang, sort = 'name') {
  const d = data?.districts;
  if (!hasDistricts(data)) return [];
  const rows = d.items.map((it) => {
    const missing = it.status === 'missing' || !it.counts;
    const counts = missing ? null : it.counts;
    const cls = missing ? 'missing' : gapClass(counts, layer);
    const { total, part } = missing ? { total: null, part: null } : layerCounts(counts, layer);
    return {
      id: it.id,
      name: it.name?.[lang] || it.name?.ru || it.id,
      status: it.status,
      boundaryDate: it.boundary_date || null,
      counts,
      part,
      total,
      cls,
      pct: typeof cls === 'number' ? pct(part, total) : null,
    };
  });
  const byName = (a, b) => a.name.localeCompare(b.name, lang);
  if (sort === 'name') {
    rows.sort(byName);
  } else {
    const dir = sort === 'share-asc' ? 1 : -1;
    rows.sort((a, b) => {
      if ((a.pct === null) !== (b.pct === null)) return a.pct === null ? 1 : -1;
      if (a.pct === null) return byName(a, b);
      return dir * (a.pct - b.pct) || byName(a, b);
    });
  }
  if (d.unassigned_cells > 0) {
    const counts = d.unassigned;
    const cls = gapClass(counts, layer);
    const { total, part } = layerCounts(counts, layer);
    rows.push({ id: null, name: null, unassigned: true, counts, part, total, cls, pct: typeof cls === 'number' ? pct(part, total) : null });
  }
  return rows;
}

// Пометка строки: stale — «граница в OSM устарела (01.04.2019)», missing — «границы нет в OSM»; иначе null
export function districtNote(row) {
  if (row.status === 'stale') return { key: 'gaps.district.stale', vars: { date: formatDate(row.boundaryDate) || '' } };
  if (row.status === 'missing') return { key: 'gaps.district.missing', vars: {} };
  return null;
}

// Полоса нажатия у контура района: Leaflet canvas ловит линию на weight/2 + tolerance (2 px) с каждой стороны,
// то есть всего weight + 4 px. Чтобы у границ нажималась ячейка, а не район, полоса — не больше четверти
// ячейки на экране и не больше 12 px; если ячейка меньше 24 px, контур не ловит нажатие (0 — снять полосу).
export const OUTLINE_TAP_TOLERANCE = 2;

export function outlineHitWeight(cellPx) {
  if (!(cellPx >= 24)) return 0;
  return Math.max(2, Math.min(12, Math.floor(cellPx / 4) - OUTLINE_TAP_TOLERANCE * 2));
}

// Район ячейки для текста по нажатию: название и пометка stale (как при нажатии на контур); null — вне районов
export function districtLabel(data, id, lang) {
  const it = id ? data?.districts?.items?.find((x) => x.id === id) : null;
  if (!it) return null;
  return {
    name: it.name?.[lang] || it.name?.ru || it.id,
    note: districtNote({ status: it.status, boundaryDate: it.boundary_date }),
  };
}
