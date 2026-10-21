// Страница «Карта пробелов данных» (T143, контракт 1.4.0): где в открытых данных OSM не хватает сведений.
// Данные — GET api/city-gaps (CityGaps); мок — ?mock=1 (web/mock/city_gaps.json), ?mock=large (≈1,5 тыс. ячеек),
// ?mock=503 и ?mock=error — состояния «не собрана» и «не загрузилась». Доли, пороги и подписи — gaps-core.js.
// Всё из данных — только через textContent; цвет дублируется числами (нажатие на ячейку и итоги по городу).
import { applyI18n, getLang, onLangChange, setLang, t } from './i18n.js';
import {
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
  layerCounts,
  legendRanges,
  pct,
} from './gaps-core.js';

const MOCK = new URLSearchParams(location.search).get('mock');
const MOCK_URLS = { large: 'mock/city_gaps_large.json' };
const DATA_URL = MOCK === null ? 'api/city-gaps' : MOCK_URLS[MOCK] || 'mock/city_gaps.json';
const ASTANA = [51.128, 71.43];
const TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

const $ = (id) => document.getElementById(id);
const state = { data: null, error: null, layer: 'hours', selected: null, map: null, cells: new Map(), paint: null };

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
  L.tileLayer(TILES, { maxZoom: 19, attribution: ATTRIBUTION }).addTo(map);
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
  state.map = map;
  return true;
}

function paintMap() {
  for (const { poly, cell } of state.cells.values()) poly.setStyle(cellStyle(cell));
  state.cells.get(state.selected)?.poly.bringToFront();
}

function selectCell(id) {
  state.selected = id;
  paintMap();
  renderCellText();
}

function renderCellText() {
  const item = state.cells.get(state.selected);
  // слой уже назван выбранным чипом — текст сам говорит, каких сведений нет
  const text = item ? say(gapText(item.cell, layer(), getLang())) : t('gaps.cellPrompt');
  $('gaps-cell').textContent = text.charAt(0).toUpperCase() + text.slice(1);
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

function renderData() {
  renderLayerChips();
  renderLegend();
  renderTotals();
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
  if (MOCK !== null) $('gaps-back').setAttribute('href', './?mock=1'); // из мока — на главную в моке
  renderStatic();
  load();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
