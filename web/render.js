// Отрисовка результатов. Все тексты с сервера вставляем через textContent: это данные, не разметка.
import { t, formatNumber } from './i18n.js';
import { dayOffsetOf } from './time.js';

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
export function renderLoading(root, { onCancel } = {}) {
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

  const warnings = answer.warnings?.length
    ? h(
        'ul',
        { class: 'warnings', 'aria-label': t('answer.warnings') },
        answer.warnings.map((w) => h('li', { class: 'warning', text: w })),
      )
    : null;

  const body = answer.cards.length
    ? [
        answer.route ? renderRoute(answer.route, byId, ctx, ctx.originName) : null,
        h(
          'ol',
          { class: 'cards' },
          answer.cards.map((c) => renderCard(c, when, ctx)),
        ),
      ]
    : [renderEmpty(ctx)];

  root.replaceChildren(
    h(
      'article',
      { class: 'answer', lang: answer.lang || null },
      head,
      warnings,
      body,
      renderDataUsed(answer, ctx),
      answer.disclaimer ? h('p', { class: 'disclaimer', text: answer.disclaimer }) : null,
    ),
  );
}

function renderEmpty(ctx) {
  const hasFilters = Boolean(ctx.request?.overrides);
  return h(
    'div',
    { class: 'empty' },
    h('p', { class: 'empty-title', text: t('empty.title') }),
    h('p', { class: 'empty-hint', text: t('empty.hint') }),
    hasFilters
      ? h('button', { type: 'button', class: 'btn btn-ghost', onclick: ctx.onClearFilters }, t('empty.clear'))
      : null,
  );
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
      items.push(
        h(
          'li',
          { class: 'tl-point' },
          h('span', { class: `tl-rank rank-${card.idea_source}`, 'aria-hidden': 'true', text: card.rank }),
          h(
            'span',
            { class: 'tl-name' },
            h('span', { class: 'visually-hidden', text: `${t('route.stop')} ${card.rank}: ` }),
            h('strong', { text: cardTitle(card) }),
            h('span', { class: 'tl-visit', text: ` · ${t('card.visit', { lo: card.visit_min_low, hi: card.visit_min_high })}` }),
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
  return h(
    'section',
    { class: 'route', 'aria-label': t('route.title') },
    h(
      'div',
      { class: 'route-head' },
      h('strong', { text: t('route.title') }),
      h('span', {
        class: 'route-total',
        text: t('route.total', { min: route.total_min, dist: formatDistance(route.total_walk_m) }),
      }),
    ),
    h('ol', { class: 'timeline' }, items),
    route.fits_budget === false ? h('p', { class: 'route-over', text: t('route.overBudget') }) : null,
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

  const li = h(
    'li',
    { class: 'card', id: `card-${card.rank}`, 'data-rank': card.rank },
    h(
      'div',
      { class: 'card-head' },
      h('span', { class: `rank rank-${card.idea_source}`, 'aria-hidden': 'true', text: card.rank }),
      h(
        'div',
        { class: 'card-title-wrap' },
        h(
          'h3',
          { class: 'card-title' },
          h('span', { class: 'visually-hidden', text: `${card.rank}. ` }),
          cardTitle(card),
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
      h('span', { text: t('card.walk', { dist: formatDistance(card.distance_m), min: card.walk_min }) }),
      h('span', { text: t('card.visit', { lo: card.visit_min_low, hi: card.visit_min_high }) }),
    ),
    card.address ? h('p', { class: 'card-address', text: card.address }) : null,
    h(
      'div',
      { class: 'badges' },
      h('span', {
        class: `badge ${poi ? 'badge-poi' : 'badge-ai'}`,
        title: poi ? t('badge.poiTitle') : t('badge.aiTitle'),
        text: poi ? t('badge.poi') : t('badge.ai'),
      }),
      h('span', { class: `badge badge-hours is-${badge.cls}`, text: badge.text }),
    ),
    h('p', { class: `hours-note is-${badge.cls}`, text: card.open_note || t('hours.noData') }),
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
      h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => ctx.onMap(card.rank) }, t('card.onMap')),
      h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => ctx.onRoute(card.rank) }, t('card.route')),
      site
        ? h('a', { class: 'btn btn-link btn-sm', href: site, target: '_blank', rel: 'noopener noreferrer' }, t('card.site'))
        : null,
    ),
  );
  li.addEventListener('mouseenter', () => ctx.onHover?.(card.rank));
  li.addEventListener('mouseleave', () => ctx.onHover?.(null));
  return li;
}

export function popupFor(card) {
  return h(
    'div',
    { class: 'popup' },
    h('strong', { text: `${card.rank}. ${cardTitle(card)}` }),
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
