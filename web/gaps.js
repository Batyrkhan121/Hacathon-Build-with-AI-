// Страница «Карта пробелов данных» (T143): где в OSM нет отметок об освещении, бордюрах, переходах, доступности
// и часах работы. Цифры — из сборки данных (api/gaps; мок — ?mock=1), логика — gaps-core.js. Всё из данных
// попадает на страницу только через textContent; цвет дублируется цифрами (нажатие на ячейку и таблица).
import { applyI18n, getLang, onLangChange, setLang, t } from './i18n.js';
import {
  GAP_METRICS,
  buildDate,
  cellSummary,
  districtRows,
  gapClass,
  isGapsData,
  legendRanges,
  pct,
} from './gaps-core.js';

const isMock = new URLSearchParams(location.search).has('mock');
const DATA_URL = isMock ? 'mock/gaps_sample.json' : 'api/gaps';
const ASTANA = [51.128, 71.43];
const TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

const $ = (id) => document.getElementById(id);
const state = { data: null, metric: 'lit', selected: null, map: null, polys: new Map() };

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

async function loadData() {
  try {
    const res = await fetch(DATA_URL, { headers: { Accept: 'application/json' } });
    if (!res.ok) return null;
    const data = await res.json();
    return isGapsData(data) ? data : null;
  } catch {
    return null; // сети нет или сборки ещё нет — честное «ещё не собрано»
  }
}

const minN = () => state.data?.min_n ?? 5;
const metricName = (m) => t(`gaps.metric.${m}`);
const districtName = (id) => {
  const d = state.data?.districts.find((x) => x.id === id);
  return d ? d.name?.[getLang()] || d.name?.ru || d.id : null;
};

// ------------------------------------------------------------------ map

function initMap() {
  if (!window.L || !window.h3 || state.map) return Boolean(state.map);
  const map = L.map($('gaps-map'), { zoomControl: false, scrollWheelZoom: false }).setView(ASTANA, 12);
  L.control.zoom({ position: 'topright' }).addTo(map);
  L.tileLayer(TILES, { maxZoom: 19, attribution: ATTRIBUTION }).addTo(map);
  const layer = L.layerGroup().addTo(map);
  for (const cell of state.data.cells) {
    let ring;
    try {
      ring = h3.cellToBoundary(cell.h3); // [[lat, lng], …]
    } catch {
      continue; // битый индекс ячейки — пропускаем, не роняем страницу
    }
    const poly = L.polygon(ring, { className: 'gap-cell', weight: 1, fillOpacity: 1 })
      .on('click', () => selectCell(cell.h3))
      .addTo(layer);
    state.polys.set(cell.h3, { poly, cell });
  }
  if (state.polys.size) {
    map.fitBounds(L.featureGroup([...state.polys.values()].map((p) => p.poly)).getBounds(), { padding: [8, 8] });
  }
  state.map = map;
  return true;
}

// Класс шкалы — атрибутом data-gap на SVG-пути: цвет и штриховка задаются в styles.css
function paintMap() {
  for (const { poly, cell } of state.polys.values()) {
    const path = poly.getElement();
    if (!path) continue;
    path.setAttribute('data-gap', String(gapClass(cell.metrics?.[state.metric], minN())));
    path.toggleAttribute('data-selected', cell.h3 === state.selected);
  }
}

function selectCell(id) {
  state.selected = id;
  paintMap();
  renderCellText();
}

function renderCellText() {
  const box = $('gaps-cell');
  const item = state.polys.get(state.selected);
  if (!item) {
    box.textContent = t('gaps.cellPrompt');
    return;
  }
  const s = cellSummary(item.cell, state.metric, minN());
  const where = districtName(item.cell.district);
  box.textContent = `${metricName(state.metric)}${where ? ` · ${where}` : ''}: ${t(s.key, { ...s.vars, min: minN() })}`;
}

// ------------------------------------------------------------------ controls, legend, table

function renderMetricChips() {
  $('gaps-metric-chips').replaceChildren(
    ...GAP_METRICS.map((m) =>
      el(
        'button',
        {
          type: 'button',
          class: 'chip',
          'aria-pressed': String(state.metric === m),
          onclick: () => {
            state.metric = m;
            renderData();
          },
        },
        metricName(m),
      ),
    ),
  );
  $('gaps-counted').textContent = t(`gaps.counted.${state.metric}`);
}

function renderLegend() {
  const swatch = (gap) => el('span', { class: 'gap-swatch', 'data-gap': gap, 'aria-hidden': 'true' });
  $('gaps-legend-list').replaceChildren(
    ...legendRanges().map((r) =>
      el('li', {}, swatch(String(r.cls)), el('span', { text: t('gaps.legendRange', { from: r.from, to: r.to }) })),
    ),
    el('li', {}, swatch('low'), el('span', { text: t('gaps.legendLow', { min: minN() }) })),
    el('li', {}, swatch('none'), el('span', { text: t('gaps.legendNone') })),
  );
}

function renderTable() {
  const rows = districtRows(state.data, state.metric, getLang(), minN());
  const share = (r) => (r.cls === 'low' || r.share === null ? t('gaps.lowShort') : `${pct(r.share)} %`);
  $('gaps-table').replaceChildren(
    el('caption', { class: 'visually-hidden', text: t('gaps.tableCaption', { metric: metricName(state.metric) }) }),
    el(
      'thead',
      {},
      el(
        'tr',
        {},
        el('th', { scope: 'col', text: t('gaps.colDistrict') }),
        el('th', { scope: 'col', class: 'num', text: t('gaps.colUnknown') }),
        el('th', { scope: 'col', class: 'num', text: t('gaps.colTotal') }),
        el('th', { scope: 'col', class: 'num', text: t('gaps.colShare') }),
      ),
    ),
    el(
      'tbody',
      {},
      rows.map((r) =>
        el(
          'tr',
          { class: r.total ? 'is-total' : null },
          el('th', { scope: 'row', text: r.total ? t('gaps.total') : r.name }),
          el('td', { class: 'num', text: String(r.unknown) }),
          el('td', { class: 'num', text: String(r.n) }),
          el('td', { class: 'num' }, el('span', { class: 'gap-swatch', 'data-gap': String(r.cls), 'aria-hidden': 'true' }), ` ${share(r)}`),
        ),
      ),
    ),
  );
}

// ------------------------------------------------------------------ page

function renderData() {
  renderMetricChips();
  renderLegend();
  renderTable();
  paintMap();
  renderCellText();
}

function renderStatic() {
  applyI18n();
  document.title = t('gaps.pageTitle');
  document.querySelectorAll('.lang button').forEach((b) => {
    b.setAttribute('aria-pressed', String(b.dataset.lang === getLang()));
  });
  const built = state.data?.built_at ? buildDate(state.data.built_at) : null;
  $('gaps-built').hidden = !built;
  if (built) $('gaps-built').textContent = t('gaps.built', { date: built });
  if (state.map) {
    const set = (sel, text) => state.map.getContainer().querySelector(sel)?.setAttribute('title', text);
    set('.leaflet-control-zoom-in', t('map.zoomIn'));
    set('.leaflet-control-zoom-out', t('map.zoomOut'));
  }
}

async function init() {
  document.querySelectorAll('.lang button').forEach((b) => b.addEventListener('click', () => setLang(b.dataset.lang)));
  onLangChange(() => {
    renderStatic();
    if (state.data) renderData();
  });
  renderStatic();
  state.data = await loadData();
  const mock = isMock || state.data?.mock === true;
  $('mock-pill').hidden = !mock;
  $('gaps-mock-note').hidden = !(state.data && mock);
  $('gaps-empty').hidden = Boolean(state.data);
  $('gaps-view').hidden = !state.data;
  if (!state.data) {
    renderStatic();
    return;
  }
  if (!initMap()) $('gaps-map-fallback').hidden = false;
  renderStatic();
  renderData();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
