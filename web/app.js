// Qadam UI: состояние формы, пресеты S1–S5, «моё место» → H3, запрос к API и отрисовка ответа.
import { t, getLang, setLang, applyI18n, onLangChange, formatNumber } from './i18n.js';
import { fetchLandmarks, fetchMeta, recommend, isMock } from './api.js';
import * as mapView from './map.js';
import * as view from './render.js';
import { astanaClock, atIso, shortDay } from './time.js';

const PRESETS = [
  { id: 'S1', landmark: 'mega_silk_way', time: '15:00' },
  { id: 'S2', landmark: 'expo', time: '11:00' },
  { id: 'S3', landmark: 'khan_shatyr', time: '12:00', flags: { with_kids: true } },
  // S4: турист в центре (у аэропорта в данных почти пусто), сервер учитывает дорогу в NQZ.
  { id: 'S4', landmark: 'baiterek', time: '16:00', flags: { tourist: true }, departure: 120 },
  { id: 'S5', landmark: 'baiterek', time: '23:30', flags: { night: true } },
];
const BUDGETS = [30, 60, 120];
// Режимы (T72) — переключатели в UserOverrides; «в помещении» остаётся обычным фильтром.
const MODES = ['tourist', 'night', 'with_kids', 'wheelchair', 'group'];
const FILTER_FLAGS = ['indoor_only'];
const FLAGS = [...MODES, ...FILTER_FLAGS];
const MODE_ICON = { tourist: '✈', night: '🌙', with_kids: '🧒', wheelchair: '♿', group: '👥' };
const DEPARTURES = [90, 120, 180, 240]; // минут до вылета (контракт: 30–1440)
const DEFAULT_DEPARTURE = 120;
const GROUP_SIZE = 4; // «нас 3–5»
const TIMES = [null, '10:00', '19:00', '23:30'];
const DEFAULT_LANDMARK = 'expo';
const H3_RES = 9;
const ASTANA_CENTER = { lat: 51.128, lon: 71.43 };
const FAR_KM = 40;

const state = {
  landmarks: [],
  location: { type: 'landmark', id: DEFAULT_LANDMARK },
  whereMsg: null, // { key, kind } — сообщение под «Где я»
  budget: null,
  flags: Object.fromEntries(FLAGS.map((f) => [f, false])),
  departure: DEFAULT_DEPARTURE,
  time: null, // null = сейчас, иначе 'HH:MM' по Астане
  day: 0, // 0 = сегодня, 1 = завтра; ведущий выбирает один раз, пресеты день не меняют
  activePreset: null,
  picking: false,
  inflight: null,
  lastRequest: null,
  lastFormSnapshot: null,
  lastAnswer: null,
  lastError: null,
  meta: null,
};

const $ = (id) => document.getElementById(id);
const els = {
  form: $('ask'),
  query: $('query'),
  submit: $('submit'),
  formError: $('form-error'),
  landmark: $('landmark'),
  geoBtn: $('geo-btn'),
  pickBtn: $('pick-btn'),
  whereNote: $('where-note'),
  presets: $('presets'),
  budgetChips: $('budget-chips'),
  flagChips: $('flag-chips'),
  modeChips: $('mode-chips'),
  departure: $('departure'),
  departureChips: $('departure-chips'),
  filterHint: $('filter-hint'),
  dayChips: $('day-chips'),
  timeChips: $('time-chips'),
  timeInput: $('time-input'),
  results: $('results'),
  status: $('status'),
  answer: $('answer'),
  mockPill: $('mock-pill'),
  stale: $('stale'),
  staleBtn: $('stale-btn'),
  mapFallback: $('map-fallback'),
};

// ------------------------------------------------------------------ helpers

function landmarkById(id) {
  return state.landmarks.find((l) => l.id === id) || null;
}

function landmarkName(lm) {
  if (!lm) return '';
  return lm.names?.[getLang()] || lm.names?.ru || lm.id;
}

function cellAreaKm2(cell) {
  try {
    return formatNumber(h3.cellArea(cell, 'km2'), 1);
  } catch {
    return formatNumber(0.1, 1);
  }
}

function haversineKm(a, b) {
  const R = 6371;
  const rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad;
  const dLon = (b.lon - a.lon) * rad;
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}

// Название старта того запроса, на который пришёл ответ (форма к этому времени могла измениться).
function requestOriginName() {
  const loc = state.lastRequest?.location;
  if (!loc) return '';
  if (loc.h3_cell) return t('map.myArea');
  return landmarkName(landmarkById(loc.landmark_id)) || loc.landmark_id;
}

function whereLabel() {
  if (state.location.type === 'cell') return t('map.myArea');
  return landmarkName(landmarkById(state.location.id)) || state.location.id;
}

// ------------------------------------------------------------------ request

function buildRequest(query, useLlm = true) {
  const location =
    state.location.type === 'cell' ? { h3_cell: state.location.cell } : { landmark_id: state.location.id };
  const overrides = {};
  if (state.budget) overrides.time_budget_min = state.budget;
  if (state.flags.with_kids) overrides.with_kids = true;
  if (state.flags.wheelchair) overrides.wheelchair = true;
  if (state.flags.indoor_only) overrides.indoor_only = true;
  if (state.flags.group) overrides.group_size = GROUP_SIZE;
  // Выключенный режим не шлём совсем (а не false): иначе он перебил бы то, что понял парсер.
  if (state.flags.tourist) {
    overrides.tourist = true;
    overrides.departure_in_min = state.departure;
  }
  if (state.flags.night) overrides.night = true;
  return {
    query,
    location,
    at: atIso(state.time, state.day),
    lang: getLang(),
    overrides: Object.keys(overrides).length ? overrides : null,
    want_route: true,
    use_llm: useLlm,
  };
}

// Сравниваем условия формы, а не текущее значение часов и способ генерации.
// «Завтра в это время» не должно устаревать только из-за смены минуты во время запроса.
function formSnapshot() {
  const body = buildRequest(els.query.value.trim());
  return JSON.stringify({
    query: body.query,
    location: body.location,
    lang: body.lang,
    overrides: body.overrides,
    time: state.time,
    day: state.day,
  });
}

async function submit({ useLlm = true } = {}) {
  const query = els.query.value.trim();
  if (!query) {
    showFormError(t('query.empty'));
    els.query.focus();
    return;
  }
  showFormError('');
  let body = buildRequest(query, useLlm);
  const snapshot = formSnapshot();

  state.inflight?.abort();
  const ctrl = new AbortController();
  state.inflight = ctrl;
  setBusy(true);
  syncStale();
  mapView.clearAnswer();
  view.renderLoading(els.answer, { onCancel: cancelRequest });
  setStatus(t('status.loading', { where: whereLabel() }));
  revealResults(); // скелетон должен быть виден сразу, а не после ответа
  const started = performance.now();

  try {
    let answer;
    try {
      answer = await recommend(body, ctrl.signal);
    } catch (err) {
      // Один повтор по тем же условиям. Отмена, новый запрос или правка формы
      // не должны запускать скрытый запрос за уже неактуальными рекомендациями.
      if (
        err.kind !== 'timeout' ||
        !body.use_llm ||
        ctrl.signal.aborted ||
        state.inflight !== ctrl ||
        snapshot !== formSnapshot()
      ) {
        throw err;
      }
      body = { ...body, use_llm: false };
      setStatus(t('status.retryNoAi'));
      answer = await recommend(body, ctrl.signal);
    }
    if (state.inflight !== ctrl) return;
    state.lastRequest = body;
    state.lastFormSnapshot = snapshot;
    state.lastAnswer = answer;
    state.lastError = null;
    renderAnswer();
    setStatus(
      t('status.done', { sec: formatNumber((performance.now() - started) / 1000, 1), n: answer.cards.length }),
    );
  } catch (err) {
    if (err.kind === 'aborted' || state.inflight !== ctrl) return;
    state.lastRequest = body;
    state.lastFormSnapshot = snapshot;
    state.lastAnswer = null;
    state.lastError = err;
    renderError(); // role="alert" сам озвучит ошибку
    setStatus('');
  } finally {
    if (state.inflight === ctrl) {
      state.inflight = null;
      setBusy(false);
      syncStale();
    }
  }
}

// Предупреждение относится только к видимым карточкам, а не к скелетону загрузки.
function syncStale() {
  const stale = state.lastAnswer && state.lastFormSnapshot !== formSnapshot();
  els.stale.hidden = Boolean(state.inflight) || !stale;
}

// Отмена: возвращаем то, что было на экране до запроса.
function cancelRequest() {
  const ctrl = state.inflight;
  if (!ctrl) return;
  state.inflight = null;
  ctrl.abort();
  setBusy(false);
  restoreResults();
  setStatus(t('status.cancelled'));
}

function restoreResults() {
  if (state.lastAnswer) renderAnswer();
  else if (state.lastError) renderError();
  else renderIntro();
  syncStale();
}

function renderIntro() {
  view.renderIntro(els.answer, { onExample: () => applyPreset(PRESETS[0]) });
}

function autoGrow() {
  const q = els.query;
  q.style.height = 'auto';
  q.style.height = `${Math.min(q.scrollHeight + 2, 168)}px`;
}

function setBusy(busy) {
  els.results.setAttribute('aria-busy', String(busy));
  els.submit.disabled = busy;
  els.submit.querySelector('span').textContent = busy ? t('submit.busy') : t('submit');
}

function setStatus(text) {
  els.status.textContent = text;
}

function showFormError(text) {
  els.formError.textContent = text;
  els.formError.hidden = !text;
  if (text) els.query.setAttribute('aria-invalid', 'true');
  else els.query.removeAttribute('aria-invalid');
}

function revealResults() {
  const wide = window.matchMedia('(min-width: 1280px)').matches;
  if (wide) return; // результаты в своей колонке, уже видны
  els.results.scrollIntoView({ behavior: mapView.reducedMotion() ? 'auto' : 'smooth', block: 'start' });
}

// ------------------------------------------------------------------ rendering

function renderAnswer({ redrawMap = true } = {}) {
  const answer = state.lastAnswer;
  if (!answer) return;
  view.renderAnswer(els.answer, answer, {
    request: state.lastRequest,
    meta: state.meta,
    originName: requestOriginName(),
    onMap: (rank) => focusOnMap(rank),
    onRoute: (rank) => showRoute(rank),
    onHover: (rank) => mapView.highlightStop(rank),
    onClearFilters: () => {
      clearFilters();
      submit();
    },
  });
  if (!redrawMap) return;
  const route = answer.route;
  mapView.showAnswer(answer, {
    originLabel: t('map.origin', { name: answer.context?.area_label || whereLabel() }),
    popupFor: view.popupFor,
    onSelect: (rank) => {
      mapView.highlightStop(rank);
      view.highlightCard(rank);
    },
    routeLabel: route
      ? t('route.total', { min: route.total_min, dist: view.formatDistance(route.total_walk_m) })
      : '',
    markerLabel: (card) =>
      `${card.rank}. ${card.title} — ${card.idea_source === 'poi_data' ? t('badge.poi') : t('badge.ai')}`,
  });
}

function renderError() {
  view.renderError(els.answer, state.lastError, {
    canRetryNoAi: state.lastRequest?.use_llm !== false,
    onRetry: () => submit({ useLlm: state.lastRequest?.use_llm !== false }),
    onRetryNoAi: () => submit({ useLlm: false }),
    onShorten: () => {
      els.query.scrollIntoView({ block: 'center', behavior: mapView.reducedMotion() ? 'auto' : 'smooth' });
      els.query.focus();
      els.query.select();
    },
  });
}

function focusOnMap(rank) {
  mapView.focusStop(rank);
  scrollMapIntoView();
}

function showRoute(rank) {
  mapView.showRoute(rank, t('route.direct'));
  scrollMapIntoView();
}

function scrollMapIntoView() {
  if (window.matchMedia('(max-width: 899px)').matches) {
    document.querySelector('.map-wrap').scrollIntoView({ behavior: mapView.reducedMotion() ? 'auto' : 'smooth' });
  }
}

function renderPresets() {
  els.presets.replaceChildren(
    ...PRESETS.map((p) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'preset';
      btn.setAttribute('aria-pressed', String(state.activePreset === p.id));
      btn.title = t(`preset.${p.id}.query`);
      const tag = document.createElement('span');
      tag.className = 'preset-tag';
      tag.textContent = p.id;
      const label = document.createElement('span');
      label.textContent = t(`preset.${p.id}.label`);
      btn.append(tag, label);
      btn.addEventListener('click', () => applyPreset(p));
      return btn;
    }),
  );
}

function chip(label, pressed, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'chip';
  b.textContent = label;
  b.setAttribute('aria-pressed', String(pressed));
  b.addEventListener('click', onClick);
  return b;
}

function renderChips() {
  els.budgetChips.replaceChildren(
    ...BUDGETS.map((m) =>
      chip(t(`budget.${m}`), state.budget === m, () => {
        state.budget = state.budget === m ? null : m;
        filtersChanged();
      }),
    ),
  );
  const toggle = (f) => () => {
    state.flags[f] = !state.flags[f];
    filtersChanged();
  };
  els.flagChips.replaceChildren(...FILTER_FLAGS.map((f) => chip(t(`flag.${f}`), state.flags[f], toggle(f))));
  els.modeChips.replaceChildren(
    ...MODES.map((f) => {
      const b = chip(t(`flag.${f}`), state.flags[f], toggle(f));
      b.prepend(Object.assign(document.createElement('span'), { className: 'chip-icon', textContent: MODE_ICON[f] }));
      b.firstChild.setAttribute('aria-hidden', 'true');
      return b;
    }),
  );
  els.departure.hidden = !state.flags.tourist;
  els.departureChips.replaceChildren(
    ...DEPARTURES.map((m) =>
      chip(t(`departure.${m}`), state.departure === m, () => {
        state.departure = m;
        filtersChanged();
      }),
    ),
  );
  els.dayChips.replaceChildren(
    ...[0, 1].map((d) =>
      chip(`${t(d ? 'when.tomorrow' : 'when.today')} · ${shortDay(d)}`, state.day === d, () => {
        state.day = d;
        filtersChanged();
      }),
    ),
  );
  // «Сейчас» завтра = то же время суток, но следующего дня.
  const nowLabel = state.day ? t('when.sameTime') : t('when.now');
  els.timeChips.replaceChildren(
    ...TIMES.map((time) =>
      chip(time ? time : `${nowLabel} · ${astanaClock()}`, state.time === time, () => {
        state.time = time;
        filtersChanged();
      }),
    ),
  );
  els.timeInput.value = state.time || '';
  const hints = [];
  if (state.flags.tourist) hints.push(t('hint.tourist'));
  if (state.flags.wheelchair) hints.push(t('hint.wheelchair'));
  els.filterHint.hidden = !hints.length;
  els.filterHint.textContent = hints.join(' ');
}

function filtersChanged() {
  state.activePreset = null;
  renderChips();
  renderPresets();
  syncStale();
}

function clearFilters() {
  state.budget = null;
  FLAGS.forEach((f) => (state.flags[f] = false));
  renderChips();
}

function renderLandmarkSelect() {
  const groups = [
    { key: 'where.groupLandmarks', items: state.landmarks.filter((l) => l.kind !== 'transport') },
    { key: 'where.groupTransport', items: state.landmarks.filter((l) => l.kind === 'transport') },
  ];
  const nodes = [];
  if (state.location.type === 'cell') {
    const opt = new Option(`⬡ ${t('where.cellOption')}`, '__cell', true, true);
    nodes.push(opt);
  }
  for (const g of groups) {
    if (!g.items.length) continue;
    const og = document.createElement('optgroup');
    og.label = t(g.key);
    g.items.forEach((l) => {
      const selected = state.location.type === 'landmark' && state.location.id === l.id;
      og.append(new Option(landmarkName(l), l.id, selected, selected));
    });
    nodes.push(og);
  }
  els.landmark.replaceChildren(...nodes);
}

function renderWhereNote() {
  const note = els.whereNote;
  note.className = 'where-note';
  if (state.whereMsg) {
    note.textContent = t(state.whereMsg.key);
    note.classList.add(`is-${state.whereMsg.kind}`);
    return;
  }
  if (state.location.type === 'cell') {
    note.textContent = t('where.noteCell', { cell: state.location.cell, area: cellAreaKm2(state.location.cell) });
    note.classList.add('is-privacy');
    if (state.location.far) {
      note.append(document.createElement('br'), t('geo.far'));
      note.classList.add('is-warn');
    }
  } else {
    note.textContent = t('where.noteLandmark', { id: state.location.id });
  }
}

function renderLocationOnMap() {
  if (state.location.type === 'cell') {
    mapView.showCell(state.location.cell, t('cell.tooltip', { area: cellAreaKm2(state.location.cell) }));
  } else {
    const lm = landmarkById(state.location.id);
    mapView.showLandmark(lm, t('map.origin', { name: landmarkName(lm) }));
  }
}

// ------------------------------------------------------------------ location

function setLocation(loc) {
  state.location = loc;
  state.whereMsg = null;
  state.activePreset = null;
  mapView.clearAnswer();
  renderLandmarkSelect();
  renderWhereNote();
  renderLocationOnMap();
  renderPresets();
  syncStale();
}

function setCellFromPoint(lat, lng) {
  // Координаты живут только внутри этой функции: дальше идёт лишь ячейка H3 r9.
  const cell = h3.latLngToCell(lat, lng, H3_RES);
  const [cLat, cLon] = h3.cellToLatLng(cell);
  const far = haversineKm(ASTANA_CENTER, { lat: cLat, lon: cLon }) > FAR_KM;
  setLocation({ type: 'cell', cell, far });
}

function onGeoClick() {
  stopPicking();
  if (!window.h3) return setWhereMsg('h3.missing', 'error');
  if (!navigator.geolocation) return setWhereMsg('geo.unsupported', 'error');
  els.geoBtn.disabled = true;
  setWhereMsg('geo.locating', 'info');
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      els.geoBtn.disabled = false;
      setCellFromPoint(pos.coords.latitude, pos.coords.longitude);
    },
    (err) => {
      els.geoBtn.disabled = false;
      setWhereMsg(err.code === 1 ? 'geo.denied' : 'geo.unavailable', 'error');
    },
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 120000 },
  );
}

function setWhereMsg(key, kind) {
  state.whereMsg = { key, kind };
  renderWhereNote();
}

function onPickClick() {
  if (state.picking) return stopPicking();
  if (!window.h3) return setWhereMsg('h3.missing', 'error');
  const ok = mapView.startPick(
    (lat, lng) => {
      state.picking = false;
      syncPickButton();
      setCellFromPoint(lat, lng);
    },
    { label: t('pick.banner'), cancelLabel: t('pick.cancel'), onCancel: stopPicking },
  );
  if (!ok) return;
  state.picking = true;
  syncPickButton();
  setWhereMsg('pick.active', 'info');
  scrollMapIntoView();
}

function stopPicking() {
  if (!state.picking) return;
  state.picking = false;
  mapView.stopPick();
  syncPickButton();
  if (state.whereMsg?.key === 'pick.active') {
    state.whereMsg = null;
    renderWhereNote();
  }
}

function syncPickButton() {
  els.pickBtn.setAttribute('aria-pressed', String(state.picking));
}

// ------------------------------------------------------------------ presets

function applyPreset(p) {
  stopPicking();
  els.query.value = t(`preset.${p.id}.query`);
  autoGrow();
  state.budget = null;
  FLAGS.forEach((f) => (state.flags[f] = Boolean(p.flags?.[f])));
  state.departure = p.departure || DEFAULT_DEPARTURE;
  state.time = p.time; // день (сегодня/завтра) пресет не трогает
  setLocation({ type: 'landmark', id: p.landmark });
  state.activePreset = p.id;
  renderChips();
  renderPresets();
  submit();
}

// ------------------------------------------------------------------ init

function bindEvents() {
  els.form.addEventListener('submit', (e) => {
    e.preventDefault();
    submit();
  });
  els.query.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });
  els.query.addEventListener('input', () => {
    autoGrow();
    if (state.activePreset) {
      state.activePreset = null;
      renderPresets();
    }
    showFormError('');
    syncStale();
  });
  els.landmark.addEventListener('change', () => {
    if (els.landmark.value === '__cell') return;
    stopPicking();
    setLocation({ type: 'landmark', id: els.landmark.value });
  });
  els.geoBtn.addEventListener('click', onGeoClick);
  els.pickBtn.addEventListener('click', onPickClick);
  els.timeInput.addEventListener('change', () => {
    state.time = els.timeInput.value || null;
    filtersChanged();
  });
  els.staleBtn.addEventListener('click', () => submit());
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') stopPicking();
  });
  document.querySelectorAll('.lang button').forEach((b) => {
    b.addEventListener('click', () => setLang(b.dataset.lang));
  });
  onLangChange(() => {
    syncLangButtons();
    renderAll();
    if (!state.inflight) {
      if (state.lastAnswer) renderAnswer({ redrawMap: false });
      else if (state.lastError) renderError();
      else renderIntro();
    }
    syncStale();
    if (state.picking) mapView.setPickLabel(t('pick.banner'), t('pick.cancel'));
    autoGrow();
    if (state.location.type === 'cell') {
      mapView.updateCellTooltip(t('cell.tooltip', { area: cellAreaKm2(state.location.cell) }));
    }
    setBusy(Boolean(state.inflight));
  });
  // Часы на чипе «Сейчас» обновляем раз в минуту.
  setInterval(() => {
    if (!state.time) renderChips();
  }, 60000);
  window.addEventListener('resize', () => mapView.invalidate());
}

function syncLangButtons() {
  document.querySelectorAll('.lang button').forEach((b) => {
    b.setAttribute('aria-pressed', String(b.dataset.lang === getLang()));
  });
  mapView.setZoomTitles(t('map.zoomIn'), t('map.zoomOut'));
}

function renderAll() {
  renderPresets();
  renderChips();
  renderLandmarkSelect();
  renderWhereNote();
}

async function init() {
  applyI18n();
  syncLangButtons();
  els.mockPill.hidden = !isMock;
  if (!mapView.initMap(document.getElementById('map'))) els.mapFallback.hidden = false;
  mapView.setZoomTitles(t('map.zoomIn'), t('map.zoomOut'));
  if (!window.h3) {
    els.geoBtn.disabled = true;
    els.pickBtn.disabled = true;
  }
  bindEvents();
  renderAll();
  renderIntro();

  try {
    state.landmarks = await fetchLandmarks();
  } catch {
    setWhereMsg('landmarks.error', 'error');
  }
  if (!landmarkById(state.location.id) && state.landmarks.length) {
    state.location = { type: 'landmark', id: state.landmarks[0].id };
  }
  renderLandmarkSelect();
  renderWhereNote();
  renderLocationOnMap();
  state.meta = await fetchMeta();
}

init();
