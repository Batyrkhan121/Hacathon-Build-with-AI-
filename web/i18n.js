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

    'unit.m': 'м',
    'unit.km': 'км',
    'unit.min': 'мин',
    'unit.ms': '{n} мс',

    'answer.noAi': 'Ответ без ИИ: правила и шаблоны',
    'answer.warnings': 'Предупреждения',
    'empty.title': 'Рядом ничего не подошло',
    'empty.hint': 'Попробуйте больше времени, другой ориентир или меньше фильтров.',
    'empty.clear': 'Сбросить фильтры и повторить',

    'card.walk': '≈{dist} · {min} мин пешком',
    'card.visit': 'на месте {lo}–{hi} мин',
    'card.why': 'Почему',
    'card.what': 'Что делать',
    'card.unnamed': 'без названия в OSM',
    'card.tags': 'Теги',
    'card.confidence': 'Полнота данных',
    'card.sources': 'Факты:',
    'card.onMap': 'На карте',
    'card.route': 'Маршрут',
    'card.site': 'Сайт',
    'badge.poi': 'Данные POI',
    'badge.poiTitle': 'Идея опирается на данные о месте',
    'badge.ai': 'Идея ИИ',
    'badge.aiTitle': 'Сгенерировано моделью, проверьте',
    'hours.open': 'Открыто (OSM)',
    'hours.openAt': 'Открыто в {time} (OSM)',
    'hours.probably': 'Вероятно открыто',
    'hours.unknown': 'Часы не подтверждены',
    'hours.closed': 'Закрыто',
    'hours.maybeClosed': 'Возможно, закрыто',
    'hours.noData': 'Нет данных о часах, проверьте перед визитом',

    'route.title': 'Мини-маршрут',
    'route.total': '≈{min} мин с посещением · {dist} пешком',
    'route.start': 'Старт',
    'route.stop': 'Остановка',
    'route.overBudget': 'Маршрут не укладывается в ваше время',
    'route.show': 'Показать на карте',
    'route.direct': 'Прямая от старта: расстояние приблизительное',

    'data.title': 'Какие данные использованы',
    'data.location': 'Место',
    'data.time': 'Время',
    'data.weather': 'Погода',
    'data.noWeather': 'нет данных о погоде',
    'data.filters': 'Фильтры',
    'data.intent': 'Как понят запрос',
    'data.sources': 'Источники',
    'data.candidates': 'Рассмотрено мест',
    'data.parser': 'Разбор запроса',
    'data.generation': 'Тексты карточек',
    'data.model': 'Модель',
    'data.latency': 'Время ответа сервера',
    'data.snapshot': 'Снимок данных: {date} · {count} POI',
    'data.sent': 'Что ушло на сервер',
    'data.sentNote': 'Координат в запросе нет: только id ориентира или ячейка H3.',
    'parser.llm': 'ИИ (Claude)',
    'parser.rules': 'правила, без ИИ',
    'gen.llm': 'ИИ по данным мест',
    'gen.template': 'шаблоны, без ИИ',

    'intent.budget': 'бюджет {n} мин',
    'intent.walk': 'пешком ≤ {n} мин',
    'intent.avoid': 'без:',
    'intent.kids': 'с ребёнком',
    'intent.wheelchair': 'доступная среда',
    'intent.indoor': 'в помещении',
    'intent.outdoor': 'на улице',
    'intent.night': 'ночь',
    'intent.tourist': 'турист',
    'intent.group': 'группа {n}',

    'cat.cafe': 'Кафе',
    'cat.restaurant': 'Ресторан',
    'cat.fast_food': 'Фастфуд',
    'cat.bar': 'Бар',
    'cat.nightlife': 'Ночная жизнь',
    'cat.museum': 'Музей',
    'cat.gallery': 'Галерея',
    'cat.theatre': 'Театр',
    'cat.cinema': 'Кино',
    'cat.library': 'Библиотека',
    'cat.park': 'Парк',
    'cat.playground': 'Детская площадка',
    'cat.viewpoint': 'Смотровая площадка',
    'cat.attraction': 'Достопримечательность',
    'cat.monument': 'Памятник',
    'cat.religious': 'Религиозное место',
    'cat.mall': 'ТРЦ',
    'cat.sport': 'Спорт',
    'cat.entertainment': 'Развлечения',
    'cat.other': 'Место',

    'tag.coffee': 'кофе',
    'tag.wifi': 'Wi-Fi',
    'tag.power_outlets': 'розетки',
    'tag.quiet': 'тихо',
    'tag.work_friendly': 'можно поработать',
    'tag.kids': 'для детей',
    'tag.interactive': 'интерактив',
    'tag.indoor': 'в помещении',
    'tag.outdoor': 'на улице',
    'tag.view': 'вид',
    'tag.photo_spot': 'для фото',
    'tag.night': 'ночью',
    'tag.late_open': 'допоздна',
    'tag.24_7': '24/7',
    'tag.free': 'бесплатно',
    'tag.wheelchair': 'доступно на коляске',
    'tag.vegetarian': 'вегетарианское',
    'tag.local_cuisine': 'местная кухня',
    'tag.live_music': 'живая музыка',
    'tag.walk': 'прогулка',
    'tag.water': 'у воды',
    'tag.architecture': 'архитектура',
    'tag.history': 'история',
    'tag.art': 'искусство',
    'tag.shopping': 'шопинг',
    'tag.sport': 'спорт',
    'tag.food': 'еда',

    'src.osm': 'OpenStreetMap',
    'src.wikidata': 'Wikidata',
    'src.wikipedia': 'Wikipedia',
    'src.google_places': 'Google Places',
    'src.manual': 'вручную',
    'src.llm_generated': 'описание ИИ',
    'src.open_meteo': 'Open-Meteo',
    'src.mock': 'МОК',
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
