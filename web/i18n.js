// Интерфейс на ru / kk / en. Тексты ответа (why, summary...) приходят с сервера на языке запроса.

const STORAGE_KEY = 'qadam.lang';
export const LANGS = ['ru', 'kk', 'en'];
const LOCALES = { ru: 'ru-RU', kk: 'kk-KZ', en: 'en-GB' };

const DICT = {
  ru: {
    'app.title': 'Qadam — прогулки по Астане',
    'app.tagline': 'ИИ-помощник для прогулок по Астане',
    'app.mock': 'Мок-данные',
    'app.mockTitle': 'Ответ берётся из web/mock/answer_sample.json, а не с сервера',
    'skip': 'К результатам',
    'lang.aria': 'Язык интерфейса',

    'presets.title': 'Попробуйте сценарий',
    'preset.S1.label': 'Прогулка у Mega',
    'preset.S1.query': 'Я возле Mega Silk Way, хочу прогуляться 30–60 минут',
    'preset.S2.label': 'Тихо, кофе, розетка',
    'preset.S2.query': 'Хочу тихое место с кофе и розеткой',
    'preset.S3.label': 'С ребёнком',
    'preset.S3.query': 'Еду с ребёнком — где рядом площадка или музей с интерактивом?',
    'preset.S4.label': '2 часа до вылета',
    'preset.S4.query': 'Я турист, у меня 2 часа перед вылетом — что успею посмотреть рядом?',
    'preset.S5.label': 'Ночью',
    'preset.S5.query': 'Ищу необычную ночную активность',

    'query.label': 'Что хотите?',
    'query.placeholder': 'Например: хочу прогуляться 30–60 минут и выпить кофе',
    'submit': 'Подсказать',
    'submit.busy': 'Ищу…',

    'where.legend': 'Где я',
    'where.landmark': 'Ориентир',
    'where.cellOption': 'Мой район (H3)',
    'where.groupLandmarks': 'Ориентиры',
    'where.groupTransport': 'Транспорт',
    'where.noteLandmark': 'На сервер уйдёт только id ориентира: {id}',
    'where.noteCell': 'На сервер уйдёт только ячейка H3 r9 {cell} (≈{area} км²). Точные координаты остаются в браузере.',
    'geo.btn': 'Моё место',
    'pick.btn': 'Указать на карте',
    'pick.active': 'Нажмите на карту…',
    'geo.locating': 'Определяю место…',
    'geo.denied': 'Доступ к геопозиции запрещён. Выберите ориентир или укажите точку на карте.',
    'geo.unavailable': 'Не удалось определить место. Выберите ориентир или укажите точку на карте.',
    'geo.unsupported': 'Браузер не умеет определять место. Выберите ориентир.',
    'geo.far': 'Похоже, вы не в Астане: подсказки работают только по Астане.',
    'h3.missing': 'Библиотека H3 не загрузилась. Выберите ориентир.',
    'cell.tooltip': 'Мы передаём только этот район ≈{area} км²',

    'filters.legend': 'Фильтры',
    'budget.aria': 'Сколько есть времени',
    'budget.30': '30 мин',
    'budget.60': '1 час',
    'budget.120': '2 часа',
    'flag.with_kids': 'С ребёнком',
    'flag.wheelchair': 'Доступная среда',
    'flag.indoor_only': 'В помещении',
    'flag.group': 'Нас 3–5',
    'hint.wheelchair': 'Доступность отмечена лишь у ≈1% мест в OSM, поэтому выбор будет узким.',

    'when.legend': 'Когда',
    'when.now': 'Сейчас',
    'when.custom': 'Своё время',
    'when.hint': 'Машина времени для демо, время Астаны (UTC+5)',

    'intro': 'Нажмите сценарий S1–S5 или опишите, чего хочется. Получите 1–3 места рядом: почему, сколько времени и что делать.',
    'query.empty': 'Напишите запрос или выберите сценарий.',
    'location.missing': 'Выберите ориентир или своё место.',
    'landmarks.error': 'Не удалось загрузить ориентиры.',

    'results.title': 'Результаты',
    'status.loading': 'Ищу места: {where}…',
    'status.done': 'Готово за {sec} с',

    'map.aria': 'Карта Астаны',
    'map.fallback': 'Карта не загрузилась. Карточки работают и без неё.',
    'map.origin': 'Старт: {name}',
    'map.myArea': 'ваш район',
  },
};

const listeners = new Set();
let current = detectInitial();

function detectInitial() {
  const fromUrl = new URLSearchParams(location.search).get('lang');
  if (LANGS.includes(fromUrl)) return fromUrl;
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (LANGS.includes(saved)) return saved;
  } catch {
    // localStorage недоступен (приватный режим) — это не ошибка
  }
  const nav = (navigator.language || 'ru').slice(0, 2).toLowerCase();
  return LANGS.includes(nav) ? nav : 'ru';
}

export function getLang() {
  return current;
}

export function locale() {
  return LOCALES[current];
}

export function t(key, vars) {
  const table = DICT[current] || DICT.ru;
  let s = table[key] ?? DICT.ru[key] ?? key;
  if (vars) {
    s = s.replace(/\{(\w+)\}/g, (m, name) => (name in vars ? String(vars[name]) : m));
  }
  return s;
}

export function setLang(lang) {
  if (!LANGS.includes(lang) || lang === current) return;
  current = lang;
  try {
    localStorage.setItem(STORAGE_KEY, lang);
  } catch {
    // язык просто не запомнится
  }
  applyI18n();
  listeners.forEach((fn) => fn(lang));
}

export function onLangChange(fn) {
  listeners.add(fn);
}

export function applyI18n(root = document) {
  document.documentElement.lang = current;
  document.title = t('app.title');
  root.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  root.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
  root.querySelectorAll('[data-i18n-aria]').forEach((el) => {
    el.setAttribute('aria-label', t(el.dataset.i18nAria));
  });
  root.querySelectorAll('[data-i18n-title]').forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
  });
}

export function formatNumber(n, digits = 0) {
  return new Intl.NumberFormat(locale(), { maximumFractionDigits: digits }).format(n);
}
