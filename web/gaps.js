// Страница «Карта пробелов данных» (T143, контракт 1.4.0): где в открытых данных OSM не хватает сведений.
// Данные — GET api/city-gaps (CityGaps); мок — ?mock=1 (web/mock/city_gaps.json), ?mock=large (≈1,5 тыс. ячеек),
// ?mock=503 и ?mock=error — состояния «не собрана» и «не загрузилась». Доли, пороги и подписи — gaps-core.js.
// Всё из данных — только через textContent; цвет дублируется числами (нажатие на ячейку и итоги по городу).
import { applyI18n, getLang, onLangChange, setLang, t } from './i18n.js';
import { TILES, applyAttribution } from './map.js';
import {
  GAP_LAYERS,
  GAP_MIN,
  attributionUrl,
  districtListChecked,
  districtName,
  districtNote,
  districtRows,
  formatBuiltAt,
  formatDate,
  formatLength,
  formatNumber,
  gapClass,
  gapText,
  hasDistricts,
  isCityGaps,
  layerById,
  layerCounts,
  legendRanges,
  pct,
} from './gaps-core.js';

const MOCK = new URLSearchParams(location.search).get('mock');
const MOCK_URLS = { large: 'mock/city_gaps_large.json' };
const DATA_URL = MOCK === null ? 'api/city-gaps' : MOCK_URLS[MOCK] || 'mock/city_gaps.json';
const ASTANA = [51.128, 71.43];

const $ = (id) => document.getElementById(id);
const state = {
  data: null,
  error: null,
  layer: 'hours',
  selected: null, // h3_cell выбранной ячейки
  selectedDistrict: null, // id района, контур которого нажали
  sort: 'name', // таблица районов: name | share-desc | share-asc
  map: null,
  cells: new Map(),
  outlines: new Map(), // id района → { lines, hits, item }
  paint: null,
};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) node.append(c);
  return node;
}

// 503 (и 404, пока маршрута нет) — «карта пока не собрана»; прочее — «не удалось загрузить» с повтором
async function loadData() {
  if (MOCK === '503') return { error: 'notBuilt' };
  if (MOCK === 'error') return { error: 'failed' };
  try {
    const res = await fetch(DATA_URL, { headers: { Accept: 'application/json' } });
    if (res.status === 503 || res.status === 404) return { error: 'notBuilt' };
    if (!res.ok) return { error: 'failed' };
    const data = await res.json();
    return isCityGaps(data) ? { data } : { error: 'failed' };
  } catch {
    return { error: 'failed' };
  }
}

const layer = () => layerById(state.layer);
const layerName = (id) => t(`gaps.layer.${id}`);
const say = (s) => t(s.key, s.vars);

// ------------------------------------------------------------------ map (canvas: ≈1,5 тыс. ячеек на телефоне)

// Цвета — токены CSS (.gaps-body), чтобы шкала жила в одном месте; «мало данных» — серая штриховка
function paintStyles() {
  const css = getComputedStyle(document.body);
  const v = (name) => css.getPropertyValue(name).trim();
  const tile = document.createElement('canvas');
  tile.width = 8;
  tile.height = 8;
  const ctx = tile.getContext('2d');
  ctx.fillStyle = v('--gap-low');
  ctx.fillRect(0, 0, 8, 8);
  ctx.strokeStyle = v('--gap-low-line');
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(-2, 10);
  ctx.lineTo(10, -2);
  ctx.moveTo(-2, 2);
  ctx.lineTo(2, -2);
  ctx.moveTo(6, 10);
  ctx.lineTo(10, 6);
  ctx.stroke();
  return {
    ramp: [0, 1, 2, 3, 4].map((i) => v(`--gap-${i}`)),
    hatch: ctx.createPattern(tile, 'repeat'),
    none: v('--line-strong'),
    edge: v('--surface'),
    selected: v('--gold'),
    outline: v('--ink'),
  };
}

function cellStyle(cell) {
  const p = state.paint;
  const cls = gapClass(cell, layer());
  const selected = cell.h3_cell === state.selected;
  const base = {
    weight: selected ? 3 : 1,
    color: selected ? p.selected : p.edge,
    dashArray: null,
    fillOpacity: 0.82,
    opacity: 1,
  };
  if (cls === 'none') return { ...base, fillOpacity: 0, color: selected ? p.selected : p.none, dashArray: '2 4' };
  if (cls === 'low') return { ...base, fillColor: p.hatch, fillOpacity: 1 };
  return { ...base, fillColor: p.ramp[cls] };
}

function initMap() {
  if (state.map) return true;
  if (!window.L || !window.h3) return false;
  state.paint = paintStyles();
  const map = L.map($('gaps-map'), { zoomControl: false, scrollWheelZoom: false, preferCanvas: true }).setView(ASTANA, 11);
  L.control.zoom({ position: 'topright' }).addTo(map);
  L.tileLayer(TILES, { maxZoom: 19 }).addTo(map); // атрибуция — applyAttribution (язык страницы, rel)
  const renderer = L.canvas({ padding: 0.3, tolerance: 2 });
  const group = L.featureGroup().addTo(map);
  for (const cell of state.data.cells) {
    let ring;
    try {
      ring = h3.cellToBoundary(cell.h3_cell); // [[lat, lng], …]
    } catch {
      continue; // битый индекс — пропускаем, страницу не роняем
    }
    const poly = L.polygon(ring, { renderer, ...cellStyle(cell) }).on('click', () => selectCell(cell.h3_cell));
    poly.addTo(group);
    state.cells.set(cell.h3_cell, { poly, cell });
  }
  if (state.cells.size) map.fitBounds(group.getBounds(), { padding: [8, 8] });
  drawOutlines(map, renderer);
  state.map = map;
  return true;
}

// 1.4.1: районы — тонкие контуры без заливки поверх ячеек (stale — пунктиром). Рисуем линиями, а не
// полигонами: на canvas полигон ловит нажатие всей площадью и закрыл бы ячейки. Для пальца у каждой линии
// невидимая полоса 12 px — нажатие рядом с границей называет район, внутри района нажимаются ячейки.
function drawOutlines(map, renderer) {
  if (!hasDistricts(state.data)) return;
  for (const item of state.data.districts.items) {
    if (item.status === 'missing' || !item.outline?.length) continue;
    const rings = item.outline.map((ring) => [...ring, ring[0]]);
    const lines = rings.map((r) => L.polyline(r, { renderer, interactive: false, ...outlineStyle(item, false) }).addTo(map));
    const hits = rings.map((r) =>
      L.polyline(r, { renderer, weight: 12, opacity: 0, interactive: true })
        .on('click', () => selectDistrict(item.id))
        .addTo(map),
    );
    state.outlines.set(item.id, { lines, hits, item });
  }
}

function outlineStyle(item, selected) {
  return {
    color: state.paint.outline,
    weight: selected ? 3 : 1.5,
    opacity: 0.9,
    dashArray: item.status === 'stale' ? '6 4' : null,
  };
}

function paintMap() {
  for (const { poly, cell } of state.cells.values()) poly.setStyle(cellStyle(cell));
  state.cells.get(state.selected)?.poly.bringToFront();
  // контуры районов и их полосы для нажатия — всегда над ячейками
  for (const [id, o] of state.outlines) {
    o.lines.forEach((l) => l.setStyle(outlineStyle(o.item, id === state.selectedDistrict)).bringToFront());
    o.hits.forEach((l) => l.bringToFront());
  }
}

function selectCell(id) {
  state.selected = id;
  state.selectedDistrict = null;
  paintMap();
  renderCellText();
}

function selectDistrict(id) {
  state.selectedDistrict = id;
  state.selected = null;
  paintMap();
  renderCellText();
}

const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function renderCellText() {
  const lang = getLang();
  const o = state.outlines.get(state.selectedDistrict);
  if (o) {
    // нажали на контур — название района (и пометка, если граница в OSM устарела)
    const note = districtNote({ status: o.item.status, boundaryDate: o.item.boundary_date });
    const name = districtName(state.data, o.item.id, lang);
    $('gaps-cell').textContent = note ? `${name} — ${say(note)}` : name;
    return;
  }
  const item = state.cells.get(state.selected);
  if (!item) {
    $('gaps-cell').textContent = t('gaps.cellPrompt');
    return;
  }
  // слой уже назван выбранным чипом — текст сам говорит, каких сведений нет; район ячейки — впереди
  const text = say(gapText(item.cell, layer(), lang));
  if (!hasDistricts(state.data)) {
    $('gaps-cell').textContent = cap(text);
    return;
  }
  const where = districtName(state.data, item.cell.district, lang) || cap(t('gaps.district.unassigned'));
  $('gaps-cell').textContent = `${where}: ${text}`;
}

// ------------------------------------------------------------------ controls, legend, totals

function renderLayerChips() {
  $('gaps-layer-chips').replaceChildren(
    ...GAP_LAYERS.map((l) =>
      el(
        'button',
        {
          type: 'button',
          class: 'chip',
          'aria-pressed': String(state.layer === l.id),
          onclick: () => {
            state.layer = l.id;
            renderData();
          },
        },
        layerName(l.id),
      ),
    ),
  );
  $('gaps-about').textContent = t(`gaps.about.${state.layer}`);
}

function renderLegend() {
  const l = layer();
  const min = l.unit === 'm' ? formatLength(GAP_MIN.m, getLang()) : GAP_MIN[l.unit];
  const swatch = (gap) => el('span', { class: 'gap-swatch', 'data-gap': gap, 'aria-hidden': 'true' });
  $('gaps-legend-list').replaceChildren(
    ...legendRanges().map((r) =>
      el('li', {}, swatch(String(r.cls)), el('span', { text: t('gaps.legendRange', { from: r.from, to: r.to }) })),
    ),
    el('li', {}, swatch('low'), el('span', { text: t(`gaps.legendLow.${l.unit}`, { min }) })),
    el('li', {}, swatch('none'), el('span', { text: t('gaps.legendNone') })),
    ...(hasDistricts(state.data)
      ? [
          el('li', {}, swatch('outline'), el('span', { text: t('gaps.legendOutline') })),
          state.data.districts.items.some((d) => d.status === 'stale')
            ? el('li', {}, swatch('outline-stale'), el('span', { text: t('gaps.legendOutlineStale') }))
            : null,
        ]
      : []),
  );
}

function renderTotals() {
  const city = state.data.city;
  const lang = getLang();
  const num = (l, n) => (l.unit === 'm' ? formatLength(n, lang) : formatNumber(n, lang));
  $('gaps-table').replaceChildren(
    el('caption', { class: 'visually-hidden', text: t('gaps.totalsCaption') }),
    el(
      'thead',
      {},
      el(
        'tr',
        {},
        el('th', { scope: 'col', text: t('gaps.colLayer') }),
        el('th', { scope: 'col', class: 'num', text: t('gaps.colUnknown') }),
        el('th', { scope: 'col', class: 'num', text: t('gaps.colTotal') }),
        el('th', { scope: 'col', class: 'num', text: t('gaps.colShare') }),
      ),
    ),
    el(
      'tbody',
      {},
      GAP_LAYERS.map((l) => {
        const { total, part } = layerCounts(city, l);
        const cls = gapClass(city, l);
        const share = typeof cls === 'number' ? `${pct(part, total)} %` : t('gaps.lowShort');
        return el(
          'tr',
          { class: l.id === state.layer ? 'is-current' : null },
          el('th', { scope: 'row', text: layerName(l.id) }),
          el('td', { class: 'num', text: num(l, part) }),
          el('td', { class: 'num', text: num(l, total) }),
          el('td', { class: 'num' }, el('span', { class: 'gap-swatch', 'data-gap': String(cls), 'aria-hidden': 'true' }), ` ${share}`),
        );
      }),
    ),
  );
}

// 1.4.1: таблица районов по выбранному слою. Сортировка — кнопками в заголовке: «Район» — по алфавиту,
// «Доля» — по доле «нет данных» (повторное нажатие меняет направление). Без блока districts таблицы нет.
function renderDistricts() {
  const box = $('gaps-districts');
  box.hidden = !hasDistricts(state.data);
  if (box.hidden) return;
  const lang = getLang();
  const l = layer();
  const d = state.data.districts;
  const num = (n) => (l.unit === 'm' ? formatLength(n, lang) : formatNumber(n, lang));
  const sortBtn = (label, onclick, arrow) =>
    el('button', { type: 'button', class: 'th-sort', onclick }, label, arrow ? el('span', { 'aria-hidden': 'true', text: arrow }) : null);
  const byShare = state.sort !== 'name';
  const shareSort = state.sort === 'share-asc' ? 'ascending' : 'descending';
  const head = el(
    'tr',
    {},
    el(
      'th',
      { scope: 'col', 'aria-sort': byShare ? 'none' : 'ascending' },
      sortBtn(t('gaps.colDistrict'), () => setSort('name'), byShare ? null : '▲'),
    ),
    el('th', { scope: 'col', class: 'num', text: t('gaps.colUnknown') }),
    el('th', { scope: 'col', class: 'num', text: t('gaps.colTotal') }),
    el(
      'th',
      { scope: 'col', class: 'num', 'aria-sort': byShare ? shareSort : 'none' },
      sortBtn(
        t('gaps.colShare'),
        () => setSort(state.sort === 'share-desc' ? 'share-asc' : 'share-desc'),
        byShare ? (state.sort === 'share-asc' ? '▲' : '▼') : null,
      ),
    ),
  );
  const shareCell = (r) => {
    const text = typeof r.cls === 'number' ? `${r.pct} %` : r.cls === 'low' ? t('gaps.lowShort') : t('gaps.noneShort');
    return el('td', { class: 'num' }, el('span', { class: 'gap-swatch', 'data-gap': String(r.cls), 'aria-hidden': 'true' }), ` ${text}`);
  };
  const rows = districtRows(state.data, l, lang, state.sort).map((r) => {
    if (r.unassigned) {
      return el(
        'tr',
        { class: 'is-unassigned' },
        el('th', { scope: 'row', text: cap(t('gaps.district.unassigned')) }),
        el('td', { class: 'num', text: num(r.part) }),
        el('td', { class: 'num', text: num(r.total) }),
        shareCell(r),
      );
    }
    const note = districtNote(r);
    if (r.status === 'missing') {
      // границы нет в OSM: ни чисел, ни догадок
      return el(
        'tr',
        { class: 'is-missing' },
        el('th', { scope: 'row', text: r.name }),
        el('td', { colspan: '3', class: 'district-note', text: say(note) }),
      );
    }
    return el(
      'tr',
      { class: r.status === 'stale' ? 'is-stale' : null },
      el('th', { scope: 'row' }, r.name, note ? el('span', { class: 'district-note', text: say(note) }) : null),
      el('td', { class: 'num', text: num(r.part) }),
      el('td', { class: 'num', text: num(r.total) }),
      shareCell(r),
    );
  });
  $('gaps-districts-table').replaceChildren(
    el('caption', { class: 'visually-hidden', text: t('gaps.districtsCaption', { layer: layerName(l.id) }) }),
    el('thead', {}, head),
    el('tbody', {}, rows),
  );
  const source = t('gaps.districtsSource', { date: formatDate(d.boundaries_date) || '' });
  const list = districtListChecked(state.data)
    ? t('gaps.listChecked', { date: formatDate(d.official_list_checked) || '' })
    : t('gaps.listUnchecked');
  $('gaps-districts-note').textContent = `${source} ${list}`;
}

function setSort(sort) {
  state.sort = sort;
  renderDistricts();
  // таблица перерисована — фокус возвращаем на нажатую кнопку сортировки
  const btns = $('gaps-districts-table').querySelectorAll('.th-sort');
  (sort === 'name' ? btns[0] : btns[1])?.focus();
}

function renderData() {
  renderLayerChips();
  renderLegend();
  renderTotals();
  renderDistricts();
  if (state.map) paintMap();
  renderCellText();
}

// ------------------------------------------------------------------ page

function renderStatic() {
  applyI18n();
  document.title = t('gaps.pageTitle');
  document.querySelectorAll('.lang button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.lang === getLang())));
  const d = state.data;
  const poi = d && formatDate(d.poi_date);
  const walk = d && formatDate(d.walk_graph_date);
  const built = d && formatBuiltAt(d.built_at);
  $('gaps-dates').hidden = !(poi && walk && built);
  if (poi && walk && built) $('gaps-dates').textContent = t('gaps.dates', { built, poi, walk });
  // атрибуция — ссылки из полей файла (copyright_url, license_url), с проверкой хоста
  if (d) {
    $('gaps-attr-osm').setAttribute('href', attributionUrl('copyright', d.copyright_url));
    $('gaps-attr-license').setAttribute('href', attributionUrl('license', d.license_url));
  }
  if (state.error) {
    $('gaps-empty-title').textContent = t(state.error === 'notBuilt' ? 'gaps.notBuiltTitle' : 'gaps.errorTitle');
    $('gaps-empty-hint').textContent = t(state.error === 'notBuilt' ? 'gaps.notBuiltHint' : 'gaps.errorHint');
  }
  if (state.map) {
    applyAttribution(state.map, t('map.attrOsm'));
    const set = (sel, text) => state.map.getContainer().querySelector(sel)?.setAttribute('title', text);
    set('.leaflet-control-zoom-in', t('map.zoomIn'));
    set('.leaflet-control-zoom-out', t('map.zoomOut'));
  }
}

async function load() {
  const { data, error } = await loadData();
  state.data = data || null;
  state.error = error || null;
  $('mock-pill').hidden = MOCK === null;
  $('gaps-mock-note').hidden = !(MOCK !== null && data);
  $('gaps-empty').hidden = Boolean(data);
  $('gaps-retry').hidden = error !== 'failed';
  $('gaps-view').hidden = !data;
  renderStatic();
  if (!data) return;
  if (!initMap()) $('gaps-map-fallback').hidden = false;
  renderStatic();
  renderData();
}

function init() {
  document.querySelectorAll('.lang button').forEach((b) => b.addEventListener('click', () => setLang(b.dataset.lang)));
  onLangChange(() => {
    renderStatic();
    if (state.data) renderData();
  });
  $('gaps-retry').addEventListener('click', () => {
    $('gaps-empty').hidden = true;
    load();
  });
  // из мока — на главную в моке: и «← К прогулкам», и логотип
  if (MOCK !== null) document.querySelectorAll('#gaps-back, .brand-home').forEach((a) => a.setAttribute('href', './?mock=1'));
  renderStatic();
  load();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
