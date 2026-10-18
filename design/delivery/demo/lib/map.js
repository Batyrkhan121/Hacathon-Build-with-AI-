// Карта Leaflet: старт (ориентир или H3-ячейка), места из ответа и мини-маршрут.
// Точную точку пользователя карта не показывает: только полигон его H3-ячейки.

const ASTANA = [51.128, 71.43];
// Стандартные тайлы OSM: CARTO basemaps с 2026 года отдают без ключа заглушку «API KEY REQUIRED».
const TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

let map = null;
let originLayer;
let cellLayer;
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
  answerLayer = L.layerGroup().addTo(map);
  directLayer = L.layerGroup().addTo(map);
  map.createPane('routeSteps');
  map.getPane('routeSteps').style.zIndex = '410';
  map.getPane('routeSteps').style.pointerEvents = 'none';
  stepLayer = L.layerGroup().addTo(map);
  map.on('zoomend viewreset resize', drawRouteSteps);
  return true;
}

function stopIcon(rank, ideaSource) {
  return L.divIcon({
    className: `stop-icon is-${ideaSource}`,
    html: `<span>${Number(rank)}</span>`,
    iconSize: [30, 34],
    iconAnchor: [15, 17],
    popupAnchor: [0, -16],
  });
}

// Ответ сервера: старт (context.origin), номерные места и мини-маршрут следами (он приблизительный).
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

export function showAnswer(answer, { originLabel, popupFor, onSelect, routeLabel, markerLabel }) {
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
    const latlngs = answer.route.path.map((p) => [p.lat, p.lon]);
    routeLine = L.polyline(latlngs, {
      // Retain geometry for fitBounds; only footsteps are visible.
      opacity: 0,
      interactive: false,
    }).addTo(answerLayer);
    stepPath = latlngs;
    pts.push(...latlngs);
  }
  for (const card of answer.cards) {
    const ll = [card.geo.lat, card.geo.lon];
    const marker = L.marker(ll, {
      icon: stopIcon(card.rank, card.idea_source),
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
    map.fitBounds(L.latLngBounds(pts), { padding: [48, 48], maxZoom: 16, animate: !reducedMotion() });
    drawRouteSteps();
  }
}

export function clearAnswer() {
  answerLayer?.clearLayers();
  directLayer?.clearLayers();
  stopMarkers.clear();
  stepPath = [];
  stepLayer?.clearLayers();
  routeLine = null;
}

export function focusStop(rank) {
  const marker = stopMarkers.get(rank);
  if (!map || !marker) return;
  highlightStop(rank);
  map.flyTo(marker.getLatLng(), Math.max(map.getZoom(), 16), { duration: reducedMotion() ? 0 : 0.5 });
  marker.openPopup();
}

// Маршрут: весь мини-маршрут, а если его нет — прямая от старта до места (честно помечена).
export function showRoute(rank, directLabel) {
  if (!map) return;
  directLayer.clearLayers();
  if (rank) highlightStop(rank);
  if (routeLine) {
    map.fitBounds(routeLine.getBounds(), { padding: [48, 48], maxZoom: 16, animate: !reducedMotion() });
    return;
  }
  const marker = stopMarkers.get(rank);
  if (!marker || !originPoint) return;
  const line = L.polyline([originPoint, marker.getLatLng()], {
    opacity: 0,
    interactive: false,
  }).addTo(directLayer);
  stepPath = [originPoint, marker.getLatLng()];
  drawRouteSteps();
  map.fitBounds(line.getBounds(), { padding: [60, 60], maxZoom: 16, animate: !reducedMotion() });
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
    color: '#245fc5',
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

// Visual hint layer in the designer preview; coordinates are the existing landmark data.
export function showLandmarkHints(landmarks) {
 if (!map || !window.L) return;
 for (const lm of landmarks) L.circleMarker([lm.geo.lat,lm.geo.lon],{radius:3,color:'#6b7280',weight:1,fillColor:'#ffffff',fillOpacity:1,interactive:false}).addTo(map);
}

// Decorative footsteps follow the approximate route; they never represent live location.
// Space in screen pixels and rebuild on zoom, keeping both route ends and stops clear.
function drawRouteSteps() {
  stepLayer?.clearLayers();
  if (!map || stepPath.length < 2) return;
  const points = stepPath.map(p => map.project(L.latLng(p)));
  const stops = [...stopMarkers.values()].map(m => map.project(m.getLatLng()));
  if (originPoint) stops.push(map.project(L.latLng(originPoint)));
  const segments = points.slice(1).map((p, i) => ({ a: points[i], b: p, length: points[i].distanceTo(p) }));
  const total = segments.reduce((sum, s) => sum + s.length, 0);
  if (total < 64) return;
  const spacing = Math.max(32, total / 120);
  let walked = 0, index = 0;
  for (const segment of segments) {
    if (segment.length < 1) continue;
    const dx = (segment.b.x - segment.a.x) / segment.length;
    const dy = (segment.b.y - segment.a.y) / segment.length;
    const angle = Math.atan2(dy, dx) * 180 / Math.PI + 90;
    for (let d = spacing / 2; d < segment.length; d += spacing) {
      const point = L.point(segment.a.x + dx * d, segment.a.y + dy * d);
      if (stops.some(stop => stop.distanceTo(point) < 32)) continue;
      const side = index++ % 2 ? 1 : -1;
      const offset = point.add(L.point(-dy * 5 * side, dx * 5 * side));
      const progress = (walked + d) / total;
      const icon = L.divIcon({
        className: 'route-step', iconSize: [12, 23], iconAnchor: [6, 11.5],
        html: `<span style="transform:rotate(${angle}deg)"><img src="../assets/search-sole.svg" alt="" style="--step-delay:${(progress * 2.4).toFixed(3)}s;--step-side:${side}" width="10" height="21"></span>`,
      });
      const marker = L.marker(map.unproject(offset), { icon, pane: 'routeSteps', interactive: false, keyboard: false }).addTo(stepLayer);
      marker.getElement()?.setAttribute('aria-hidden', 'true');
    }
    walked += segment.length;
  }
}
