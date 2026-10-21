// Отрисовка результатов. Все тексты с сервера вставляем через textContent: это данные, не разметка.
import { t, formatNumber } from './i18n.js';
import { dayOffsetOf } from './time.js';
import { dayDiff, eventStatus, eventStopLabel, eventWhen, localParts } from './events.js';
import { visitLabel } from './visit.js';

// подпись из чистых модулей (events.js, visit.js): ключ i18n + параметры
const eventLabel = (st) => t(st.key, st.vars);

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const CATEGORY_ICON = {
  cafe: '☕',
  restaurant: '🍽️',
  fast_food: '🍔',
  bar: '🍸',
  nightlife: '🌙',
  museum: '🏛️',
  gallery: '🖼️',
  theatre: '🎭',
  cinema: '🎬',
  library: '📚',
  park: '🌳',
  playground: '🎠',
  viewpoint: '🔭',
  attraction: '🎡',
  monument: '🗿',
  religious: '🛐',
  mall: '🛍️',
  sport: '⚽',
  entertainment: '🎳',
  other: '📍',
};

// В OSM 110 из 111 площадок без имени: пайплайн подставляет родовое название.
const GENERIC_TITLES = new Set(['детская площадка', 'балалар алаңы', 'playground', 'площадка']);

export function categoryLabel(cat) {
  return t(`cat.${cat}`) === `cat.${cat}` ? t('cat.other') : t(`cat.${cat}`);
}

export function formatDistance(m) {
  if (m >= 1000) return `${formatNumber(m / 1000, 1)} ${t('unit.km')}`;
  return `${formatNumber(Math.max(10, Math.round(m / 10) * 10))} ${t('unit.m')}`;
}

function cardTitle(card) {
  return card.title?.trim() || categoryLabel(card.category);
}

// Сервер различает безымянные места суффиксом: «Детская площадка · 350 м» — смотрим часть до « · ».
function isUnnamed(card) {
  const base = card.title?.split(' · ')[0].trim().toLowerCase();
  return !base || GENERIC_TITLES.has(base);
}

function sourceName(src) {
  const key = `src.${src}`;
  return t(key) === key ? src : t(key);
}

function tagLabel(tag) {
  const key = `tag.${tag}`;
  return t(key) === key ? tag : t(key);
}

function safeUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === 'https:' || u.protocol === 'http:' ? u.href : null;
  } catch {
    return null;
  }
}

// Для какого момента считались часы: время из `at` и «завтра» ли это.
// Время в бейдже — то, на которое сервер считал часы (context.local_time), а не то, что ушло в запросе:
// на живом API они совпадают, а мок отвечает своим фиксированным временем.
function whenOf(request, answer) {
  if (!request?.at) return { time: null, tomorrow: false }; // «сейчас» — без времени в бейдже
  const at = answer?.context?.local_time || request.at;
  return { time: at.slice(11, 16), tomorrow: dayOffsetOf(at) === 1 };
}

// «Открыто» — только при open + high (правило продукта 2).
export function hoursBadge(card, when = {}) {
  const s = card.open_status;
  const c = card.open_confidence;
  if (s === 'open' && c === 'high') {
    if (!when.time) return { cls: 'open', text: t('hours.open') };
    const key = when.tomorrow ? 'hours.openAtTomorrow' : 'hours.openAt';
    return { cls: 'open', text: t(key, { time: when.time }) };
  }
  if (s === 'open' && c === 'medium') return { cls: 'maybe', text: t('hours.probably') };
  if (s === 'closed') return { cls: 'closed', text: c === 'low' ? t('hours.maybeClosed') : t('hours.closed') };
  return { cls: 'unknown', text: t('hours.unknown') };
}

// ------------------------------------------------------------------ states

// Пустое состояние: одно полезное действие вместо пересказа функций.
export function renderIntro(root, { onExample } = {}) {
  root.replaceChildren(
    h(
      'div',
      { class: 'intro' },
      h('p', { text: t('intro') }),
      onExample ? h('button', { type: 'button', class: 'btn btn-primary', onclick: onExample }, t('intro.example')) : null,
    ),
  );
}

// Скелетон повторяет форму карточек, чтобы ничего не прыгало, когда придёт ответ.
export function renderLoading(root, { onCancel, context = [] } = {}) {
  const skel = () =>
    h(
      'li',
      { class: 'card skeleton' },
      h('div', { class: 'card-head' }, h('span', { class: 'rank sk' }), h('span', { class: 'sk sk-title' })),
      h('span', { class: 'sk sk-line' }),
      h('span', { class: 'sk sk-line short' }),
      h('span', { class: 'sk sk-block' }),
    );
  root.replaceChildren(
    h(
      'div',
      { class: 'loading', 'aria-label': t('loading.label') },
      onCancel
        ? h('button', { type: 'button', class: 'btn btn-link btn-sm loading-cancel', onclick: onCancel }, t('loading.cancel'))
        : null,
      // v2.1: что учтено в запросе — чипами; без v2 скрыто
      context.length
        ? h(
            'p',
            { class: 'loading-context v2-only' },
            h('span', { class: 'loading-context-label', text: t('loading.context') }),
            context.map((c) => h('span', { class: 'loading-chip', text: c })),
          )
        : null,
      h('span', { class: 'sk sk-summary', 'aria-hidden': 'true' }),
      h('ol', { class: 'cards', 'aria-hidden': 'true' }, skel(), skel(), skel()),
    ),
  );
}

export function errorText(err) {
  switch (err?.kind) {
    case 'timeout':
      return t('error.timeout');
    case 'validation':
      return t('error.validation', { detail: err.detail || '422' });
    case 'too_large':
      return t('error.tooLarge');
    case 'unavailable':
      return t('error.unavailable');
    case 'server':
      return t('error.server', { status: err.status ?? '?' });
    case 'format':
      return t('error.format');
    case 'mock':
      return t('error.mock');
    default:
      return t('error.network');
  }
}

// Повтор без ИИ помогает только при сбоях ИИ/сети; при 413, 422 и 503 тот же запрос не пройдёт.
const NO_AI_USELESS = new Set(['too_large', 'validation', 'unavailable']);

export function renderError(root, err, { canRetryNoAi, onRetry, onRetryNoAi, onShorten }) {
  const tooLarge = err?.kind === 'too_large';
  const offerNoAi = canRetryNoAi && !NO_AI_USELESS.has(err?.kind);
  root.replaceChildren(
    h(
      'div',
      { class: 'error-box', role: 'alert' },
      h('p', { class: 'error-title', text: tooLarge ? t('error.tooLargeTitle') : t('error.title') }),
      h('p', { class: 'error-text', text: errorText(err) }),
      h(
        'div',
        { class: 'error-actions' },
        tooLarge
          ? h('button', { type: 'button', class: 'btn btn-primary', onclick: onShorten }, t('error.shorten'))
          : null,
        offerNoAi
          ? h('button', { type: 'button', class: 'btn btn-primary', onclick: onRetryNoAi }, t('error.retryNoAi'))
          : null,
        tooLarge ? null : h('button', { type: 'button', class: 'btn btn-ghost', onclick: onRetry }, t('error.retry')),
      ),
      offerNoAi ? h('p', { class: 'hint', text: t('error.noAiHint') }) : null,
    ),
  );
}

// ------------------------------------------------------------------ answer

export function renderAnswer(root, answer, ctx) {
  const when = whenOf(ctx.request, answer);
  const byId = new Map(answer.cards.map((c) => [c.poi_id, c]));
  const noAi = answer.data_used?.generation === 'template';

  const head = h(
    'div',
    { class: 'answer-head' },
    h('p', { class: 'summary', text: answer.summary }),
    noAi ? h('span', { class: 'pill pill-noai', text: t('answer.noAi') }) : null,
  );

  const warm = renderWarmOffer(answer, ctx);
  const noCafe = renderNoCafeConflict(answer, ctx);
  const warnings = answer.warnings?.length
    ? h(
        'ul',
        { class: 'warnings', 'aria-label': t('answer.warnings') },
        answer.warnings.map((w) => h('li', { class: 'warning', text: w })),
      )
    : null;

  // 1.3.1: событие, до которого больше 30 мин (от context.local_time), — не остановка «сейчас», а отдельный
  // блок «Вечером рядом»; остальные события — последними среди карточек (как и в маршруте — последней остановкой).
  const nowIso = answer.context?.local_time;
  const nums = stopNumbers(answer);
  const cardCtx = { ...ctx, origin: answer.context?.origin, nowIso, stopNo: (c) => nums.get(c.rank) ?? null };
  // 8.2: событие другого дня (дата starts_at по UTC+5 ≠ дате local_time) — не «Вечером рядом», а свой блок
  // «Завтра рядом» с подписью «завтра в 19:00». Порядок событий — по времени (Date), не по строке ISO.
  const byStart = (a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at);
  const otherDay = answer.cards.filter((c) => isOtherDayEvent(c, nowIso)).sort(byStart);
  const later = answer.cards.filter((c) => !otherDay.includes(c) && isLaterEvent(c, nowIso)).sort(byStart);
  const nowCards = answer.cards.filter((c) => !later.includes(c) && !otherDay.includes(c));
  // карточки с номером — в порядке остановок, без номера (событие вне маршрута) — после них
  const order = (c) => cardCtx.stopNo(c) ?? Infinity;
  nowCards.sort((a, b) => order(a) - order(b) || (a.kind === 'event') - (b.kind === 'event') || a.rank - b.rank);
  const body = answer.cards.length
    ? [
        answer.route ? renderRoute(answer.route, byId, cardCtx, ctx.originName) : null,
        nowCards.length ? h('ol', { class: 'cards' }, nowCards.map((c) => renderCard(c, when, cardCtx))) : null,
        eventSection('later-events', laterTitle(later), later, (c) => renderCard(c, when, cardCtx)),
        eventSection('other-day-events', otherDayTitle(otherDay, nowIso), otherDay, (c) =>
          renderCard(c, when, cardCtx),
        ),
      ]
    : [renderEmpty(ctx)];

  root.replaceChildren(
    h(
      'article',
      { class: 'answer', lang: answer.lang || null },
      head,
      warm,
      noCafe,
      warnings,
      body,
      renderRefineChips(answer, ctx),
      renderDataUsed(answer, ctx),
      answer.disclaimer ? h('p', { class: 'disclaimer', text: answer.disclaimer }) : null,
    ),
  );
}

// 8.2: номер — только у остановок маршрута, по их порядку и без пропусков (без маршрута — у мест по порядку).
// Событие вне маршрута — значок календаря, другое место вне маршрута — без номера. card.rank остаётся
// внутренним ключом (id карточки, метка на карте, подсветка).
export function stopNumbers(answer) {
  const nums = new Map();
  const stops = answer.route?.stop_ids || [];
  let n = 0;
  for (const c of [...answer.cards].sort((a, b) => a.rank - b.rank)) {
    if (stops.length) {
      const i = stops.indexOf(c.poi_id);
      nums.set(c.rank, i >= 0 ? i + 1 : null);
    } else {
      nums.set(c.rank, c.kind === 'event' ? null : ++n);
    }
  }
  return nums;
}

// Знак номера (карточка, таймлайн): цифра остановки или значок календаря у события вне маршрута
function rankMark(card, no, cls) {
  const off = no == null ? (card.kind === 'event' ? ' is-event' : ' is-off') : '';
  return h('span', {
    class: `${cls} rank-${card.idea_source}${off}`,
    'aria-hidden': 'true',
    text: no == null ? '' : String(no),
  });
}

// 1.3.0, «в тепле»: при морозе — плашка-предложение повторить запрос с меткой warm (разово, без сохранения).
// Температура и «гулять ок» — из answer.context.weather (может быть null — тогда плашки нет).
const FROST_C = -10;

function isFrost(weather) {
  if (!weather || typeof weather.temp_c !== 'number') return false;
  return weather.temp_c <= FROST_C || (weather.temp_c < 0 && weather.outdoor_ok === false);
}

function renderWarmOffer(answer, ctx) {
  const weather = answer.context?.weather;
  if (!isFrost(weather) || !ctx.onWarm) return null;
  const req = ctx.request || {};
  if (req.prefs?.likes?.includes('warm') || req.overrides?.indoor_only) return null; // уже «в тепле»
  const temp = `${weather.temp_c > 0 ? '+' : weather.temp_c < 0 ? '−' : ''}${Math.abs(Math.round(weather.temp_c))}${' '}°C`;
  const leg = answer.route?.max_outdoor_leg_min;
  return h(
    'div',
    { class: 'warm-offer', role: 'note' },
    h('p', { class: 'warm-offer-text', text: t('warm.offer', { temp }) }),
    leg ? h('p', { class: 'warm-offer-leg', text: t('warm.leg', { min: leg }) }) : null,
    h('button', { type: 'button', class: 'btn btn-primary warm-offer-btn', onclick: ctx.onWarm }, t('warm.btn')),
  );
}

// 1.3.0, событие: «сегодня в 19:00–21:00», площадка (address), кнопка «Источник» (source_url, только https).
// «Идёт до 21:00», «проходит до 30.11 18:00», «началось в 19:00» — events.js (правило ARCHITECTURE §5);
// «сейчас» — context.local_time. Часы работы у события не показываем: вместо них — время события.


const LATER_MIN = 30; // до начала больше 30 мин — событие «позже», а не остановка «сейчас»

function isLaterEvent(card, nowIso) {
  if (card.kind !== 'event' || !card.starts_at || !nowIso) return false;
  return Date.parse(card.starts_at) - Date.parse(nowIso) > LATER_MIN * 60000;
}

function isOtherDayEvent(card, nowIso) {
  if (card.kind !== 'event' || !card.starts_at || !nowIso) return false;
  return Date.parse(card.starts_at) > Date.parse(nowIso) && localParts(card.starts_at).date !== localParts(nowIso).date;
}

// «Вечером рядом», если первое (по времени) из событий начинается с 17:00 по Астане; иначе «Позже рядом».
// cards уже отсортированы по Date.
function laterTitle(cards) {
  if (!cards.length) return '';
  const hour = Number(localParts(cards[0].starts_at).time.slice(0, 2));
  return t(hour >= 17 ? 'event.laterEvening' : 'event.later');
}

// «Завтра рядом», если все события — завтра; иначе «В другие дни»
function otherDayTitle(cards, nowIso) {
  if (!cards.length) return '';
  const today = localParts(nowIso).date;
  const tomorrow = cards.every((c) => dayDiff(localParts(c.starts_at).date, today) === 1);
  return t(tomorrow ? 'event.tomorrowNearby' : 'event.otherDays');
}

function eventSection(cls, title, cards, render) {
  if (!cards.length) return null;
  return h(
    'section',
    { class: `later-events ${cls}`, 'aria-labelledby': `${cls}-title` },
    h('h3', { class: 'later-events-title', id: `${cls}-title`, text: title }),
    h('ol', { class: 'cards' }, cards.map(render)),
  );
}

function eventDomain(card) {
  const src = card.source_url ? httpsOnly(card.source_url) : null;
  if (!src) return '';
  return new URL(src).hostname.replace(/^www\./, '');
}

// 8.2: источник события — афиша, а не «наша база мест»: «Из афиши · домен» и подсказка про афишу
function listingsLabel(card) {
  const domain = eventDomain(card);
  return domain ? t('event.fromListings', { domain }) : t('event.fromListingsBare');
}

function listingsTitle(card) {
  const domain = eventDomain(card);
  return domain ? t('event.listingsTitle', { domain }) : t('event.listingsTitleBare');
}

// Плашка начавшегося события: зелёная — только у идущего сеанса, иначе нейтральная
function renderEventStatus(card, nowIso) {
  const st = eventStatus(card, nowIso);
  if (!st) return null;
  return h('span', { class: `event-now${st.live ? '' : ' is-neutral'}`, text: cap(t(st.key, st.vars)) });
}

function httpsOnly(url) {
  try {
    const u = new URL(url);
    return u.protocol === 'https:' ? u.href : null;
  } catch {
    return null;
  }
}

function renderEventBlock(card, ctx) {
  const src = card.source_url ? httpsOnly(card.source_url) : null;
  return h(
    'div',
    { class: 'event-block' },
    h(
      'p',
      { class: 'event-when' },
      h('span', { class: 'event-badge', text: t('event.badge') }),
      h('span', { class: 'event-time', text: eventLabel(eventWhen(card, ctx.nowIso)) }),
      renderEventStatus(card, ctx.nowIso),
    ),
    card.venue || card.address
      ? h('p', { class: 'event-venue', text: t('event.venue', { place: card.venue || card.address }) })
      : null,
    src
      ? h(
          'a',
          {
            class: 'btn btn-ghost event-source',
            href: src,
            target: '_blank',
            rel: 'noopener noreferrer',
            'aria-label': t('event.sourceAria', { title: cardTitle(card) }),
          },
          t('event.source'),
        )
      : null,
  );
}

// 1.3.0, чипы уточнения под ответом: «ближе» → overrides.max_walk_min (≤ 120), «без кафе» → prefs.avoids cafes,
// «ещё» → exclude_poi_ids (показанные в серии места). Разовый повтор запроса, поправки складываются, без сохранения.
function renderRefineChips(answer, ctx) {
  if (!answer.cards.length || !ctx.onCloser) return null;
  const req = ctx.request || {};
  const maxWalk = Math.max(...answer.cards.map((c) => c.walk_min || 0));
  const closer = Math.min(120, Math.max(5, Math.round(maxWalk * 0.6)));
  const chips = [];
  if (maxWalk > 5 && closer < maxWalk) {
    chips.push(
      h('button', { type: 'button', class: 'chip', onclick: () => ctx.onCloser(closer) }, t('refineChip.closer')),
    );
  }
  if (!req.prefs?.avoids?.includes('cafes')) {
    chips.push(h('button', { type: 'button', class: 'chip', onclick: ctx.onNoCafe }, t('refineChip.noCafe')));
  }
  // «Ещё» (exclude_poi_ids): скрыт, когда в серии показано уже 9 мест
  if (ctx.onMore) {
    chips.push(h('button', { type: 'button', class: 'chip', onclick: ctx.onMore }, t('refineChip.more')));
  }
  if (!chips.length) return null;
  return h(
    'div',
    { class: 'refine-chips', role: 'group', 'aria-label': t('refineChip.aria') },
    h('span', { class: 'refine-chips-label', text: t('refineChip.label') }),
    chips,
  );
}

// «Без кафе» включено (чип или «Мои предпочтения»), а кафе в ответе всё равно есть (кофе в тексте запроса
// или сервер не учёл метку) — говорим об этом прямо, без догадок о причине.
function renderNoCafeConflict(answer, ctx) {
  const avoidsCoffee = ctx.request?.prefs?.avoids?.includes('cafes');
  if (!avoidsCoffee || !answer.cards.some((c) => c.category === 'cafe')) return null;
  return h('p', { class: 'warning no-cafe-conflict', role: 'note', text: t('noCafe.conflict') });
}

// Тёплая остановка — тег indoor у карточки (нового поля в 1.3.0 нет)
export function isWarmStop(card) {
  return Array.isArray(card.tags) && card.tags.includes('indoor');
}

function warmMark() {
  return h(
    'span',
    { class: 'warm-mark', title: t('warm.mark') },
    h('span', { class: 'warm-icon', 'aria-hidden': 'true' }),
    h('span', { class: 'visually-hidden', text: t('warm.mark') }),
  );
}

function renderEmpty(ctx) {
  const hasFilters = Boolean(ctx.request?.overrides);
  return h(
    'div',
    { class: 'empty' },
    h('p', { class: 'empty-title', text: t('empty.title') }),
    h('p', { class: 'empty-hint', text: t('empty.hint') }),
    h(
      'div',
      { class: 'empty-actions' },
      hasFilters
        ? h('button', { type: 'button', class: 'btn btn-ghost', onclick: ctx.onClearFilters }, t('empty.clear'))
        : null,
      ctx.onEditQuery
        ? h('button', { type: 'button', class: 'btn btn-ghost', onclick: ctx.onEditQuery }, t('empty.edit'))
        : null,
    ),
  );
}

const CROSSING_KINDS = ['signals', 'marked', 'unmarked', 'unknown']; // schemas 1.3.0 CrossingKind

// Маршрут по графу OSM: «По тротуарам: 1,2 км · 16 мин · переходов 3, со светофором 2 · без лестниц»
// + честная приписка. Числа считает браузер из полей ответа (legs, crossings, steps_count), не модель.
function renderGraphSummary(route, ctx) {
  const walkMin = route.legs.reduce((sum, leg) => sum + (leg.walk_min || 0), 0);
  // Считаем те же переходы, что рисует карта: только виды из контракта.
  const crossings = (route.crossings || []).filter((c) => CROSSING_KINDS.includes(c.kind));
  const signals = crossings.filter((c) => c.kind === 'signals').length;
  const base = crossings.length
    ? t('route.graphSummary', {
        dist: formatDistance(route.total_walk_m),
        min: walkMin,
        n: crossings.length,
        s: signals,
      })
    : t('route.graphSummaryNoCross', { dist: formatDistance(route.total_walk_m), min: walkMin });
  // text — факт «по тротуарам» в заголовке свёрнутого маршрута; ступени — отдельным фактом; caveat — внутри
  return {
    text: base,
    caveat: h('p', { class: 'route-caveat', text: t('route.osmCaveat') }),
  };
}

function stepsText(route, ctx) {
  const steps = route.steps_count || 0;
  // Лестницы важны для коляски: если просили «доступная среда» или «с ребёнком» — говорим прямо.
  const strollerMode = Boolean(ctx.request?.overrides?.wheelchair || ctx.request?.overrides?.with_kids);
  return steps
    ? `${t('route.steps', { n: steps })}${strollerMode ? ` — ${t('route.stepsHint')}` : ''}`
    : t('route.noSteps');
}

const pct = (x) => Math.round((x || 0) * 100);
// Доля «освещено» — вниз, как у CORE: «нет данных» не выдаём за «освещено»
const pctFloor = (x) => Math.floor((x || 0) * 100 + 1e-9);

// 1.3.1, итог маршрута — короткие факты с иконками, по одному на строку (цвет ink-2; бирюзовым — только
// ссылки и кнопки). Все числа — из ответа (их считает код на сервере).
// «Без ступенек» — только по графу, при нуле ступеней и straight_share = 0: на прямых отрезках ступени не проверены.
// Освещение — только при route.profile = lit («Ночью — по крупным улицам»), «по данным OSM».
// Бордюры — только из kerb_lowered / kerb_unknown (по всему пути), из обрезанного списка crossings не считаем.
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

function routeFact(kind, text, extra = '') {
  return h('span', { class: `route-fact is-${kind}${extra}`, text: cap(text) });
}

function renderRouteFacts(route, ctx, graphText) {
  const graph = route.path_source === 'graph';
  const facts = [
    routeFact('time', t('route.total', { min: route.total_min, dist: formatDistance(route.total_walk_m) })),
  ];
  if (graphText) facts.push(routeFact('walk', graphText));
  if (graph && route.steps_count === 0 && route.straight_share === 0) {
    facts.push(routeFact('steps', t('route.stepFree')));
  } else if (graph && route.steps_count > 0) {
    facts.push(routeFact('steps', stepsText(route, ctx), ' is-warn'));
  }
  if (graph && route.profile === 'accessible' && route.kerb_lowered != null) {
    const kerbs = [];
    if (route.kerb_lowered > 0) kerbs.push(t('route.kerbs', { n: route.kerb_lowered }));
    if (route.kerb_unknown) kerbs.push(t('route.kerbsUnknown', { n: route.kerb_unknown }));
    if (kerbs.length) facts.push(routeFact('kerb', kerbs.join(', ')));
  }
  if (route.profile === 'lit' && route.lit_share != null) {
    const lit =
      !graph || (route.unknown_lit_share ?? 1) >= 0.995
        ? t('route.litNoData')
        : [
            t('route.litShare', { lit: pctFloor(route.lit_share) }),
            pct(route.unknown_lit_share) > 0 ? t('route.litUnknownShare', { n: pct(route.unknown_lit_share) }) : null,
          ]
            .filter(Boolean)
            .join(', ');
    facts.push(routeFact('lit', lit));
  }
  if (route.fits_budget === false) facts.push(routeFact('over', t('route.overBudget'), ' is-warn'));
  return facts;
}

// Таймлайн: старт → (отрезок) → остановка → … Маршрут может быть петлёй: последний отрезок в origin.
function renderRoute(route, byId, ctx, originName) {
  const items = [
    h(
      'li',
      { class: 'tl-point tl-origin' },
      h('span', { class: 'tl-dot', 'aria-hidden': 'true' }),
      h('span', { class: 'tl-name' }, h('strong', { text: t('route.start') }), originName ? ` · ${originName}` : null),
    ),
  ];
  for (const leg of route.legs) {
    items.push(
      h('li', { class: 'tl-leg', text: t('route.leg', { dist: formatDistance(leg.distance_m), min: leg.walk_min }) }),
    );
    const card = leg.to_id === 'origin' ? null : byId.get(leg.to_id);
    if (card) {
      // номер — из stop_ids; отрезок к карточке не из stop_ids (рассинхрон ответа) — без номера, не «Остановка null»
      const no = ctx.stopNo ? ctx.stopNo(card) : null;
      items.push(
        h(
          'li',
          { class: 'tl-point' },
          rankMark(card, no, 'tl-rank'),
          h(
            'span',
            { class: 'tl-name' },
            h('span', { class: 'visually-hidden', text: no == null ? `${t('route.stop')}: ` : `${t('route.stop')} ${no}: ` }),
            h('strong', { text: cardTitle(card) }),
            isWarmStop(card) ? warmMark() : null,
            h('span', {
              class: 'tl-visit',
              text:
                card.kind === 'event' && card.starts_at
                  ? ` · ${eventLabel(eventStopLabel(card, ctx.nowIso))}`
                  : ` · ${eventLabel(visitLabel(card.visit_min_low, card.visit_min_high))}`,
            }),
          ),
        ),
      );
    } else {
      items.push(
        h(
          'li',
          { class: 'tl-point tl-origin' },
          h('span', { class: 'tl-dot', 'aria-hidden': 'true' }),
          h('span', { class: 'tl-name' }, h('strong', { text: t('route.back') })),
        ),
      );
    }
  }
  // Телефон: маршрут свёрнут до итога (время, метры, переходы, лестницы, превышение бюджета),
  // чтобы «Почему» первой карточки попало на первый экран ответа; таймлайн — по касанию. ≥ 900 px — открыт.
  const graph = route.path_source === 'graph' ? renderGraphSummary(route, ctx) : null;
  return h(
    'details',
    { class: 'route', open: window.matchMedia('(min-width: 900px)').matches },
    h(
      'summary',
      { class: 'route-head' },
      h('strong', { text: t('route.title') }),
      renderRouteFacts(route, ctx, graph ? graph.text : null),
    ),
    h('ol', { class: 'timeline' }, items),
    graph ? graph.caveat : null,
    h(
      'div',
      { class: 'route-foot' },
      h('p', { class: 'route-note', text: route.note }),
      h('button', { type: 'button', class: 'btn btn-link', onclick: () => ctx.onRoute(null) }, t('route.show')),
    ),
  );
}

function renderCard(card, when, ctx) {
  const badge = hoursBadge(card, when);
  const poi = card.idea_source === 'poi_data';
  const pct = Math.round((card.data_confidence ?? 0) * 100);
  const site = card.website ? safeUrl(card.website) : null;
  const unnamed = isUnnamed(card);
  const isEvent = card.kind === 'event';
  const no = ctx.stopNo ? ctx.stopNo(card) : card.rank;

  const li = h(
    'li',
    { class: 'card', id: `card-${card.rank}`, 'data-rank': card.rank },
    h(
      'div',
      { class: 'card-head' },
      rankMark(card, no, 'rank'),
      h(
        'div',
        { class: 'card-title-wrap' },
        h(
          'h3',
          { class: 'card-title' },
          no == null ? null : h('span', { class: 'visually-hidden', text: `${no}. ` }),
          cardTitle(card),
          isWarmStop(card) ? warmMark() : null,
        ),
        h(
          'p',
          { class: 'card-cat' },
          h('span', { 'aria-hidden': 'true', text: CATEGORY_ICON[card.category] || CATEGORY_ICON.other }),
          // У безымянного места заголовок и так равен категории — не повторяем её.
          unnamed
            ? h('span', { class: 'unnamed', text: ` ${t('card.unnamed')}` })
            : ` ${categoryLabel(card.category)}`,
        ),
      ),
    ),
    h(
      'p',
      { class: 'card-facts' },
      h('span', { class: 'fact-walk', text: t('card.walk', { dist: formatDistance(card.distance_m), min: card.walk_min }) }),
      h('span', { class: 'fact-visit', text: eventLabel(visitLabel(card.visit_min_low, card.visit_min_high)) }),
      // v2.1: статус часов — в строке фактов (тот же текст и та же логика, что у бейджа; бейдж скрыт в v2)
      isEvent ? null : h('span', { class: `fact-hours v2-only is-${badge.cls}`, text: badge.text }),
    ),
    isEvent ? renderEventBlock(card, ctx) : null,
    card.address && !isEvent ? h('p', { class: 'card-address', text: card.address }) : null,
    h(
      'div',
      { class: 'badges' },
      h('span', {
        class: `badge ${poi ? 'badge-poi' : 'badge-ai'}`,
        title: isEvent ? listingsTitle(card) : poi ? t('badge.poiTitle') : t('badge.aiTitle'),
        text: isEvent ? listingsLabel(card) : poi ? t('badge.poi') : t('badge.ai'),
      }),
      isEvent ? null : h('span', { class: `badge badge-hours is-${badge.cls}`, text: badge.text }),
    ),
    isEvent ? null : h('p', { class: `hours-note is-${badge.cls}`, text: card.open_note || t('hours.noData') }),
    poi ? null : h('p', { class: 'ai-note', text: t('badge.aiTitle') }),
    h(
      'dl',
      { class: 'card-text' },
      h('dt', { text: t('card.why') }),
      h('dd', { text: card.why }),
      h('dt', { text: t('card.what') }),
      h('dd', { text: card.what_to_do }),
    ),
    card.tags?.length
      ? h(
          'ul',
          { class: 'tags', 'aria-label': t('card.tags') },
          card.tags.slice(0, 6).map((tag) => h('li', { text: tagLabel(tag) })),
        )
      : null,
    // Главное действие критического пути — «Как пройти»: сразу после «почему / что делать».
    renderDirections(card, ctx.origin, ctx.preciseStart, poi),
    h(
      'div',
      { class: 'card-meta' },
      h(
        'div',
        { class: 'confidence' },
        h('span', { class: 'confidence-label', text: t('card.confidence') }),
        h(
          'span',
          {
            class: 'bar',
            role: 'meter',
            'aria-valuemin': 0,
            'aria-valuemax': 100,
            'aria-valuenow': pct,
            'aria-label': t('card.confidence'),
          },
          h('i', { class: pct >= 70 ? 'is-high' : pct >= 40 ? 'is-mid' : 'is-low', style: `width:${pct}%` }),
        ),
        h('span', { class: 'confidence-val', text: `${pct}%` }),
      ),
      h('p', { class: 'sources', text: `${t('card.sources')} ${card.facts_sources.map(sourceName).join(' · ')}` }),
    ),
    h(
      'div',
      { class: 'card-actions' },
      h('button', { type: 'button', class: 'btn btn-link btn-sm', onclick: () => ctx.onMap(card.rank) }, t('card.onMap')),
      h('button', { type: 'button', class: 'btn btn-link btn-sm', onclick: () => ctx.onRoute(card.rank) }, t('card.route')),
      site
        ? h('a', { class: 'btn btn-link btn-sm', href: site, target: '_blank', rel: 'noopener noreferrer' }, t('card.site'))
        : null,
    ),
  );
  li.addEventListener('mouseenter', () => ctx.onHover?.(card.rank));
  li.addEventListener('mouseleave', () => ctx.onHover?.(null));
  return li;
}

// «Как пройти»: пешеходный маршрут в 2GIS и Яндекс Картах, в новой вкладке.
// Без начальной точки оба сервиса маршрут не строят (ждут геопозицию), поэтому старт — центр поиска
// из ответа (context.origin: ориентир или центр H3-ячейки), от него же посчитаны минуты в карточке.
// Наша страница к этим сервисам не обращается: уходит только переход по ссылке по клику.
const coord = (x) => Number(x).toFixed(6);

export function directionsUrls(dest, origin) {
  const d = { lat: coord(dest.lat), lon: coord(dest.lon) };
  const o = origin ? { lat: coord(origin.lat), lon: coord(origin.lon) } : null;
  // Яндекс: rtext=широта,долгота~широта,долгота; rtt=pd — пешком.
  const yandex = `https://yandex.kz/maps/?rtext=${o ? `${o.lat},${o.lon}` : ''}~${d.lat},${d.lon}&rtt=pd`;
  // 2GIS: точки «долгота,широта» через «|», пешеходный режим — вкладка tab/pedestrian.
  const twogis =
    'https://2gis.kz/astana/directions/tab/pedestrian/points/' +
    `${o ? `${o.lon}%2C${o.lat}` : ''}%7C${d.lon}%2C${d.lat}`;
  return { yandex, twogis };
}

// preciseStart() отдаёт точную GPS-точку (только в памяти страницы) или null.
// В href всегда лежит старт context.origin; точную точку подставляем лишь в момент клика —
// сами открываем навигатор, ссылка в DOM не меняется (точки нет в разметке и в предпросмотре).
function renderDirections(card, origin, preciseStart, poi = false) {
  const { yandex, twogis } = directionsUrls(card.geo, origin);
  const title = cardTitle(card);
  const fromHere = Boolean(preciseStart?.());
  const link = (href, app, key) => {
    const a = h(
      'a',
      {
        class: 'btn btn-ghost btn-sm directions-btn',
        href,
        target: '_blank',
        rel: 'noopener noreferrer',
        'aria-label': t(fromHere ? 'dir.ariaHere' : 'dir.aria', { title, app }),
      },
      app,
    );
    const open = (e) => {
      const p = preciseStart?.();
      if (!p) return; // нет точной точки — обычная ссылка от context.origin
      e.preventDefault();
      window.open(directionsUrls(card.geo, p)[key], '_blank', 'noopener,noreferrer');
    };
    a.addEventListener('click', open);
    a.addEventListener('auxclick', (e) => {
      if (e.button === 1) open(e); // средняя кнопка тоже открывает от вашей точки
    });
    return a;
  };
  return h(
    'div',
    { class: 'directions' },
    h('span', { class: 'directions-label', text: t(fromHere ? 'dir.titleHere' : 'dir.title') }),
    // v2.1: источник места мелко справа от «Как пройти» (бейдж источника в v2 скрыт; «Идея ИИ» остаётся бейджем)
    card.kind === 'event'
      ? h('span', { class: 'dir-source v2-only', text: listingsLabel(card), title: listingsTitle(card) })
      : poi
        ? h('span', { class: 'dir-source v2-only', text: t('badge.poi'), title: t('badge.poiTitle') })
        : null,
    link(twogis, t('dir.2gis'), 'twogis'),
    link(yandex, t('dir.yandex'), 'yandex'),
  );
}

export function popupFor(card, no = card.rank) {
  return h(
    'div',
    { class: 'popup' },
    h(
      'strong',
      { class: 'popup-title' },
      // событие вне маршрута — без номера, но со значком календаря, как на карточке и метке
      no == null && card.kind === 'event' ? h('span', { class: 'cal-icon', 'aria-hidden': 'true' }) : null,
      `${no == null ? '' : `${no}. `}${cardTitle(card)}`,
    ),
    h('span', { text: t('card.walk', { dist: formatDistance(card.distance_m), min: card.walk_min }) }),
  );
}

export function highlightCard(rank) {
  const el = document.getElementById(`card-${rank}`);
  if (!el) return;
  el.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'nearest' });
  el.classList.remove('is-flash');
  void el.offsetWidth; // перезапуск анимации
  el.classList.add('is-flash');
}

// ------------------------------------------------------------------ data used

function row(label, value) {
  if (value === null || value === undefined || value === '' || (Array.isArray(value) && !value.length)) {
    value = '—';
  }
  return [
    h('dt', { text: label }),
    h('dd', {}, Array.isArray(value) ? h('ul', { class: 'inline-list' }, value.map((v) => h('li', { text: v }))) : value),
  ];
}

function intentChips(intent) {
  if (!intent) return [];
  const out = [];
  if (intent.time_budget_min) out.push(t('intent.budget', { n: intent.time_budget_min }));
  if (intent.max_walk_min) out.push(t('intent.walk', { n: intent.max_walk_min }));
  (intent.categories || []).forEach((c) => out.push(categoryLabel(c)));
  (intent.must_tags || []).forEach((tag) => out.push(tagLabel(tag)));
  (intent.avoid_tags || []).forEach((tag) => out.push(`${t('intent.avoid')} ${tagLabel(tag)}`));
  if (intent.with_kids) out.push(t('intent.kids'));
  if (intent.wheelchair) out.push(t('intent.wheelchair'));
  if (intent.indoor === 'indoor') out.push(t('intent.indoor'));
  if (intent.indoor === 'outdoor') out.push(t('intent.outdoor'));
  if (intent.night) out.push(t('intent.night'));
  if (intent.tourist) out.push(t('intent.tourist'));
  if (intent.group_size) out.push(t('intent.group', { n: intent.group_size }));
  return out;
}

function renderDataUsed(answer, ctx) {
  const d = answer.data_used || {};
  const parser = d.parser === 'llm' ? t('parser.llm') : t('parser.rules');
  const gen = d.generation === 'llm' ? t('gen.llm') : t('gen.template');
  const meta = ctx.meta;
  return h(
    'details',
    { class: 'data-used' },
    h('summary', {}, t('data.title')),
    h(
      'dl',
      {},
      row(t('data.location'), d.location),
      row(t('data.time'), d.time),
      row(t('data.weather'), d.weather || t('data.noWeather')),
      row(t('data.filters'), d.filters),
      row(t('data.intent'), intentChips(answer.intent)),
      row(t('data.sources'), d.sources),
      row(t('data.candidates'), String(d.candidates_considered ?? '—')),
      row(t('data.parser'), parser),
      row(t('data.generation'), gen),
      row(t('data.model'), d.model),
      row(t('data.latency'), answer.latency_ms ? t('unit.ms', { n: formatNumber(answer.latency_ms) }) : null),
    ),
    meta?.snapshot_date
      ? h('p', { class: 'snapshot', text: t('data.snapshot', { date: meta.snapshot_date, count: meta.poi_count ?? '—' }) })
      : null,
    ctx.request
      ? h(
          'div',
          { class: 'sent' },
          h('p', { class: 'sent-title', text: t('data.sent') }),
          h('pre', { class: 'sent-json', tabindex: 0, text: JSON.stringify(ctx.request, null, 2) }),
          h('p', { class: 'sent-note', text: t('data.sentNote') }),
        )
      : null,
  );
}
