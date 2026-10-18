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

// Подпись к пунктиру: он прямой, чтобы оценить расстояние; настоящий пеший маршрут — в навигаторе.
// Постоянная легенда в углу карты (на телефоне нет hover) + тот же текст в подсказке у линии.
let lineLegend = null;

function showLineLegend(text) {
  if (!map) return;
  if (!lineLegend) {
    lineLegend = L.control({ position: 'bottomleft' });
    lineLegend.onAdd = () => {
      const el = L.DomUtil.create('div', 'line-legend');
      L.DomUtil.create('span', 'line-swatch', el).setAttribute('aria-hidden', 'true');
      L.DomUtil.create('span', 'line-text', el);
      L.DomEvent.disableClickPropagation(el);
      return el;
    };
    lineLegend.addTo(map);
  }
  lineLegend.getContainer().querySelector('.line-text').textContent = text;
}

function hideLineLegend() {
  lineLegend?.remove();
  lineLegend = null;
}

export function setLineCaption(text) {
  if (lineLegend) showLineLegend(text);
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

export function showAnswer(answer, { originLabel, popupFor, onSelect, routeLabel, markerLabel, lineCaption }) {
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
    L.polyline(latlngs, { color: '#ffffff', weight: 8, opacity: 0.9, interactive: false }).addTo(answerLayer);
    routeLine = L.polyline(latlngs, {
      color: '#0a6e87',
      weight: 4,
      dashArray: '2 9',
      lineCap: 'round',
    }).addTo(answerLayer);
    routeLine.bindTooltip(lineTooltip(routeLabel, lineCaption), { sticky: true });
    showLineLegend(lineCaption);
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
  }
}

export function clearAnswer() {
  answerLayer?.clearLayers();
  directLayer?.clearLayers();
  stopMarkers.clear();
  routeLine = null;
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
export function showRoute(rank, lineCaption) {
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
    color: '#0a6e87',
    weight: 4,
    dashArray: '2 9',
    lineCap: 'round',
  }).addTo(directLayer);
  line.bindTooltip(lineTooltip(lineCaption), { sticky: true });
  showLineLegend(lineCaption);
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
