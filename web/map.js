// Карта Leaflet: старт (ориентир или H3-ячейка), места из ответа и мини-маршрут.
// GPS stays in browser memory; the server receives only the H3 cell or landmark.

const ASTANA = [51.128, 71.43];
// Стандартные тайлы OSM: CARTO basemaps с 2026 года отдают без ключа заглушку «API KEY REQUIRED».
const TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

let map = null;
let originLayer;
let cellLayer;
let hereLayer; // «вы здесь»: точная точка GPS, только на этой карте, никуда не отправляется
let answerLayer;
let directLayer;
let routeLine = null;
let stepLayer;
let stepPath = [];
let originPoint = null;
const stopMarkers = new Map(); // rank -> marker
let pickHandler = null;

export function initMap(el) {
  if (!window.L) return false;
  map = L.map(el, { zoomControl: false, attributionControl: true }).setView(ASTANA, 12);
  L.control.zoom({ position: 'topright' }).addTo(map);
  L.tileLayer(TILES, { maxZoom: 19, attribution: ATTRIBUTION }).addTo(map);
  originLayer = L.layerGroup().addTo(map);
  cellLayer = L.layerGroup().addTo(map);
  hereLayer = L.layerGroup().addTo(map);
  answerLayer = L.layerGroup().addTo(map);
  directLayer = L.layerGroup().addTo(map);
  const pane = map.createPane('routeSteps');
  pane.style.zIndex = '410';
  pane.style.pointerEvents = 'none';
  stepLayer = L.layerGroup().addTo(map);
  map.on('zoomend viewreset resize', drawRouteSteps);
  return true;
}

function stopIcon(rank, ideaSource, first) {
  return L.divIcon({
    className: `stop-icon is-${ideaSource}${first ? ' is-first' : ''}`,
    // v2.1 (эталон): золотой шестиугольник с контуром --ink; номер — textContent, без HTML из данных
    html: document.body.classList.contains('v2') ? hexStop(rank) : `<span>${Number(rank)}</span>`,
    iconSize: [34, 34],
    iconAnchor: [17, 17],
    popupAnchor: [0, -16],
  });
}

const SVG_NS = 'http://www.w3.org/2000/svg';

function hexStop(rank) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('width', '30');
  svg.setAttribute('height', '34');
  svg.setAttribute('viewBox', '0 0 34 38');
  svg.setAttribute('aria-hidden', 'true');
  const hex = document.createElementNS(SVG_NS, 'polygon');
  hex.setAttribute('class', 'stop-hex');
  hex.setAttribute('points', '17,1.5 32,10 32,28 17,36.5 2,28 2,10');
  const num = document.createElementNS(SVG_NS, 'text');
  num.setAttribute('class', 'stop-num');
  num.setAttribute('x', '17');
  num.setAttribute('y', '24.5');
  num.setAttribute('text-anchor', 'middle');
  num.textContent = String(Number(rank));
  svg.append(hex, num);
  return svg;
}

// Ответ сервера: старт (context.origin), номерные места и мини-маршрут пунктиром (он приблизительный).
// Подписи кнопок зума Leaflet на языке интерфейса (по умолчанию они английские).
export function setZoomTitles(zoomIn, zoomOut) {
  if (!map) return;
  const set = (sel, text) => {
    const a = map.getContainer().querySelector(sel);
    if (a) {
      a.title = text;
      a.setAttribute('aria-label', text);
    }
  };
  set('.leaflet-control-zoom-in', zoomIn);
  set('.leaflet-control-zoom-out', zoomOut);
}

// Легенда в углу карты (на телефоне нет hover): что за линия и какие переходы на пути.
// Граф OSM (path_source=graph) — сплошная линия по тротуарам; прямые отрезки — пунктир «по прямой…».
// labels = { straight, graph, crossings: { signals, marked, unmarked } } — тексты на языке интерфейса.
const CROSSING_KINDS = ['signals', 'marked', 'unmarked'];
let lineLegend = null;
let legendSpec = null; // { solid: boolean, kinds: string[] }
let legendLabels = null;

// HTML значка собирается только из белого списка: kind приходит из ответа сервера, а divIcon и
// легенда пишут строку через innerHTML. Неизвестный вид → пустая строка (значок не рисуется).
const CROSSING_GLYPHS = {
  signals: '<span class="crossing-glyph is-signals" aria-hidden="true">🚦</span>',
  marked: '<span class="crossing-glyph is-marked" aria-hidden="true"></span>',
  unmarked: '<span class="crossing-glyph is-unmarked" aria-hidden="true">!</span>',
};

function crossingGlyph(kind) {
  // светофор — эмодзи, зебра — полосы, без разметки — пунктирный круг с «!»
  return Object.hasOwn(CROSSING_GLYPHS, kind) ? CROSSING_GLYPHS[kind] : '';
}

function renderLegend() {
  if (!map || !legendSpec || !legendLabels) return;
  if (!lineLegend) {
    lineLegend = L.control({ position: 'bottomleft' });
    lineLegend.onAdd = () => {
      const el = L.DomUtil.create('div', 'line-legend');
      L.DomEvent.disableClickPropagation(el);
      return el;
    };
    lineLegend.addTo(map);
  }
  const el = lineLegend.getContainer();
  el.replaceChildren();
  const row = (glyphHtml, text) => {
    const r = L.DomUtil.create('div', 'legend-row', el);
    r.insertAdjacentHTML('afterbegin', glyphHtml);
    L.DomUtil.create('span', 'legend-text', r).textContent = text;
  };
  const solid = legendSpec.solid;
  row(
    `<span class="line-swatch${solid ? ' is-solid' : ''}" aria-hidden="true"></span>`,
    solid ? legendLabels.graph : legendLabels.straight,
  );
  if (!legendSpec.kinds.length) return;
  // Виды переходов: на компьютере — по строке с полной подписью, на телефоне — одной строкой
  // с короткой (CSS выбирает .kind-full или .kind-short), чтобы легенда не закрывала маршрут.
  const kinds = L.DomUtil.create('div', 'legend-kinds', el);
  legendSpec.kinds.forEach((kind) => {
    const item = L.DomUtil.create('span', 'legend-kind', kinds);
    item.insertAdjacentHTML('afterbegin', crossingGlyph(kind));
    L.DomUtil.create('span', 'kind-full', item).textContent = legendLabels.crossings[kind];
    L.DomUtil.create('span', 'kind-short', item).textContent = legendLabels.crossingsShort[kind];
  });
}

function showLegend(spec, labels) {
  legendSpec = spec;
  if (labels) legendLabels = labels;
  renderLegend();
}

function hideLineLegend() {
  lineLegend?.remove();
  lineLegend = null;
  legendSpec = null;
}

// Смена языка: перерисовать легенду и подписи переходов теми же видами.
export function setLegendLabels(labels) {
  legendLabels = labels;
  if (legendSpec) renderLegend();
}

// Отступы для fitBounds: снизу добавляем высоту легенды, чтобы конец маршрута не прятался под ней.
function fitOptions(pad) {
  const legendH = lineLegend?.getContainer().offsetHeight || 0;
  return {
    paddingTopLeft: [pad, pad],
    paddingBottomRight: [pad, Math.max(pad, legendH + 24)],
    maxZoom: 16,
    animate: !reducedMotion(),
  };
}

function crossingIcon(kind) {
  return L.divIcon({
    className: 'crossing-icon',
    html: crossingGlyph(kind),
    iconSize: [20, 20],
    iconAnchor: [10, 10],
  });
}

function lineTooltip(...lines) {
  const el = document.createElement('div');
  lines.filter(Boolean).forEach((line) => {
    const row = document.createElement('div');
    row.textContent = line;
    el.append(row);
  });
  return el;
}

export function showAnswer(answer, { originLabel, popupFor, onSelect, routeLabel, markerLabel, legend }) {
  if (!map) return;
  clearAnswer();
  const pts = [];
  const hasCell = cellLayer.getLayers().length > 0;
  originPoint = answer.context?.origin ? [answer.context.origin.lat, answer.context.origin.lon] : null;
  if (originPoint) {
    pts.push(originPoint);
    if (!hasCell) showOrigin(answer.context.origin, originLabel);
  }
  if (answer.route?.path?.length >= 2) {
    const route = answer.route;
    const graph = route.path_source === 'graph';
    const latlngs = route.path.map((p) => [p.lat, p.lon]);
    L.polyline(latlngs, { color: '#ffffff', weight: graph ? 9 : 8, opacity: 0.9, interactive: false }).addTo(
      answerLayer,
    );
    // graph: путь по тротуарам — сплошной; straight: прямые отрезки — пунктир (это оценка, не дорога).
    routeLine = L.polyline(latlngs, {
      color: '#9dbbe9',
      weight: 7,
      dashArray: graph ? null : '12 8',
      lineCap: 'round',
      lineJoin: 'round',
    }).addTo(answerLayer);
    routeLine.bindTooltip(lineTooltip(routeLabel, graph ? legend.graph : legend.straight), { sticky: true });
    stepPath = latlngs;
    const crossings = graph ? route.crossings || [] : [];
    for (const c of crossings) {
      if (!CROSSING_KINDS.includes(c.kind)) continue; // контракт: signals | marked | unmarked
      const label = legend.crossings[c.kind];
      L.marker([c.lat, c.lon], {
        icon: crossingIcon(c.kind),
        title: label,
        keyboard: false, // до 50 значков — не засоряем порядок Tab; число переходов есть в тексте маршрута
        interactive: false,
        zIndexOffset: -200,
      }).addTo(answerLayer);
    }
    const kinds = CROSSING_KINDS.filter((k) => crossings.some((c) => c.kind === k));
    showLegend({ solid: graph, kinds }, legend);
    pts.push(...latlngs);
  }
  for (const card of answer.cards) {
    const ll = [card.geo.lat, card.geo.lon];
    const marker = L.marker(ll, {
      icon: stopIcon(card.rank, card.idea_source, answer.route?.stop_ids?.[0] === card.poi_id),
      // Цвет метки (золото / лаванда) дублируем словами: «Данные POI» или «Идея ИИ».
      title: markerLabel(card),
      alt: markerLabel(card),
      riseOnHover: true,
    })
      .bindPopup(popupFor(card), { closeButton: false })
      .on('click', () => onSelect(card.rank))
      // С клавиатуры Leaflet шлёт keypress (Enter открывает попап), а не click.
      .on('keypress', (e) => {
        if (e.originalEvent?.key === 'Enter' || e.originalEvent?.keyCode === 13) onSelect(card.rank);
      })
      .addTo(answerLayer);
    stopMarkers.set(card.rank, marker);
    pts.push(ll);
  }
  if (pts.length) {
    map.fitBounds(L.latLngBounds(pts), fitOptions(48));
  }
  drawRouteSteps();
}

export function clearAnswer() {
  answerLayer?.clearLayers();
  directLayer?.clearLayers();
  stopMarkers.clear();
  routeLine = null;
  stepPath = [];
  stepLayer?.clearLayers();
  hideLineLegend();
}

export function focusStop(rank) {
  const marker = stopMarkers.get(rank);
  if (!map || !marker) return;
  highlightStop(rank);
  map.flyTo(marker.getLatLng(), Math.max(map.getZoom(), 16), { duration: reducedMotion() ? 0 : 0.5 });
  marker.openPopup();
}

// Маршрут: весь мини-маршрут, а если его нет — прямая от старта до места (честно помечена).
export function showRoute(rank, legend) {
  if (!map) return;
  directLayer.clearLayers();
  if (rank) highlightStop(rank);
  if (routeLine) {
    map.fitBounds(routeLine.getBounds(), fitOptions(48));
    return;
  }
  const marker = stopMarkers.get(rank);
  if (!marker || !originPoint) return;
  const line = L.polyline([originPoint, marker.getLatLng()], {
    color: '#9dbbe9',
    weight: 7,
    dashArray: '12 8',
    lineCap: 'round',
  }).addTo(directLayer);
  line.bindTooltip(lineTooltip(legend.straight), { sticky: true });
  stepPath = [originPoint, marker.getLatLng()];
  drawRouteSteps();
  showLegend({ solid: false, kinds: [] }, legend);
  map.fitBounds(line.getBounds(), fitOptions(60));
}

export function highlightStop(rank) {
  stopMarkers.forEach((m, r) => {
    m.getElement()?.classList.toggle('is-active', r === rank);
  });
}

export function hasMap() {
  return map !== null;
}

export function invalidate() {
  map?.invalidateSize();
}

function originIcon() {
  return L.divIcon({
    className: 'origin-icon',
    html: '<span></span>',
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
}

export function showOrigin(geo, label) {
  if (!map) return;
  originLayer.clearLayers();
  if (!geo) return;
  L.marker([geo.lat, geo.lon], { icon: originIcon(), keyboard: false, zIndexOffset: 500 })
    .bindTooltip(label, { direction: 'top', offset: [0, -10] })
    .addTo(originLayer);
}

export function showLandmark(lm, label) {
  if (!map) return;
  clearCell();
  if (!lm?.geo) return;
  showOrigin(lm.geo, label);
  map.flyTo([lm.geo.lat, lm.geo.lon], 15, { duration: reducedMotion() ? 0 : 0.6 });
}

export function showCell(cell, tooltip) {
  if (!map || !window.h3) return;
  clearCell();
  originLayer.clearLayers();
  const ring = h3.cellToBoundary(cell); // [[lat, lng], ...]
  const poly = L.polygon(ring, {
    color: '#0a6e87',
    weight: 2,
    dashArray: '6 4',
    fillColor: '#00afca',
    fillOpacity: 0.18,
  }).addTo(cellLayer);
  poly.bindTooltip(tooltip, { permanent: true, direction: 'top', className: 'cell-tooltip', offset: [0, -6] });
  map.fitBounds(poly.getBounds(), { maxZoom: 16, padding: [40, 40], animate: !reducedMotion() });
}

export function updateCellTooltip(text) {
  cellLayer?.eachLayer((layer) => layer.setTooltipContent?.(text));
}

// Синяя точка и круг точности GPS. Рисуются в браузере; координаты не уходят в API,
// в ссылку — только по клику «Как пройти».
export function showHere(lat, lng, accuracyM, label) {
  if (!map) return;
  hereLayer.clearLayers();
  L.circle([lat, lng], {
    radius: accuracyM,
    color: '#1a73e8',
    weight: 1,
    fillColor: '#1a73e8',
    fillOpacity: 0.08,
    interactive: false,
  }).addTo(hereLayer);
  L.circleMarker([lat, lng], {
    radius: 6,
    color: '#ffffff',
    weight: 2,
    fillColor: '#1a73e8',
    fillOpacity: 1,
  })
    .bindTooltip(label, { direction: 'bottom', offset: [0, 6] })
    .addTo(hereLayer);
}

export function setHereLabel(label) {
  hereLayer?.eachLayer((layer) => layer.setTooltipContent?.(label));
}

export function clearHere() {
  hereLayer?.clearLayers();
}

export function clearCell() {
  cellLayer?.clearLayers();
}

// Режим «указать на карте»: следующий клик превращается в H3-ячейку (координаты дальше не идут).
// Подсказка висит прямо на карте: на телефоне панель с кнопкой может быть вне экрана.
let pickBanner = null;

export function startPick(onPick, { label, cancelLabel, onCancel }) {
  if (!map) return false;
  stopPick();
  const container = map.getContainer();
  container.classList.add('is-picking');
  pickBanner = L.DomUtil.create('div', 'pick-banner', container);
  pickBanner.setAttribute('role', 'status');
  const text = L.DomUtil.create('span', 'pick-text', pickBanner);
  const cancel = L.DomUtil.create('button', 'pick-cancel', pickBanner);
  cancel.type = 'button';
  text.textContent = label;
  cancel.textContent = cancelLabel;
  L.DomEvent.disableClickPropagation(pickBanner);
  L.DomEvent.on(cancel, 'click', () => onCancel());
  pickHandler = (e) => {
    stopPick();
    onPick(e.latlng.lat, e.latlng.lng);
  };
  map.once('click', pickHandler);
  return true;
}

export function setPickLabel(label, cancelLabel) {
  if (!pickBanner) return;
  pickBanner.querySelector('.pick-text').textContent = label;
  pickBanner.querySelector('.pick-cancel').textContent = cancelLabel;
}

export function stopPick() {
  if (!map) return;
  map.getContainer().classList.remove('is-picking');
  if (pickHandler) map.off('click', pickHandler);
  pickHandler = null;
  pickBanner?.remove();
  pickBanner = null;
}

export function reducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
}

// Decorative direction cues use the returned path, never the GPS position or an invented route.
// Screen-space spacing keeps the graph's short segments from creating dense clusters.
function drawRouteSteps() {
  stepLayer?.clearLayers();
  if (!map || stepPath.length < 2) return;
  const points = stepPath.map(p => map.project(L.latLng(p)));
  const stops = [...stopMarkers.values()].map(m => map.project(m.getLatLng()));
  if (originPoint) stops.push(map.project(L.latLng(originPoint)));
  const segments = points.slice(1).map((p, i) => ({ a: points[i], b: p, length: points[i].distanceTo(p) }));
  const total = segments.reduce((sum, segment) => sum + segment.length, 0);
  if (total < 64) return;
  const spacing = Math.max(36, total / 120);
  let walked = 0, next = spacing / 2, index = 0;
  for (const segment of segments) {
    if (segment.length < .01) continue;
    const dx = (segment.b.x - segment.a.x) / segment.length;
    const dy = (segment.b.y - segment.a.y) / segment.length;
    const angle = Math.atan2(dy, dx) * 180 / Math.PI + 90;
    while (next < walked + segment.length) {
      const d = next - walked;
      const point = L.point(segment.a.x + dx * d, segment.a.y + dy * d);
      next += spacing;
      if (stops.some(stop => stop.distanceTo(point) < 32)) continue;
      const side = index++ % 2 ? 1 : -1;
      const offset = point.add(L.point(-dy * 1.5 * side, dx * 1.5 * side));
      const icon = L.divIcon({
        className: 'route-step', iconSize: [14, 28], iconAnchor: [7, 14],
        html: `<span style="transform:rotate(${angle}deg)"><img src="assets/search-sole.svg" alt="" style="--step-delay:${(next / total * 2.4).toFixed(3)}s;--step-side:${side}" width="12" height="25"></span>`,
      });
      const marker = L.marker(map.unproject(offset), { icon, pane: 'routeSteps', interactive: false, keyboard: false }).addTo(stepLayer);
      marker.getElement()?.setAttribute('aria-hidden', 'true');
    }
    walked += segment.length;
  }
}
