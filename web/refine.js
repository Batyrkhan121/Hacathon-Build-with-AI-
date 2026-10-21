// «Без кафе» и категория «кафе» в запросе, без DOM и без i18n (проверяется в node, web/tests).
// Решение 21.10 (вариант B): категорию UI не выбирает — её разбирает сервер из текста (answer.intent.categories;
// overrides.categories учитываем, если когда-нибудь появится). Сервер: явное «без кафе» сильнее разбора текста —
// кафе не ищет, даже если запрос про кафе. Конфликт — только когда запрос про одни кафе («хочу кофе»):
// у «где поесть» (кафе, ресторан, фастфуд) «Без кафе» — нормальное уточнение.

// 1.3.1: раздельные словари (LikeTag / AvoidTag в app/schemas.py) и противоречивые пары (PREF_CONFLICTS):
// вместе их сервер не примет (422): в «Моих предпочтениях» второй чип пары неактивен
export const LIKE_TAGS = ['quiet', 'coffee', 'kids', 'museums', 'parks', 'warm', 'night'];
export const AVOID_TAGS = ['bars', 'noisy', 'long_outdoor', 'kids_places', 'cafes'];
export const PREF_CONFLICT = { kids: 'kids_places', kids_places: 'kids', coffee: 'cafes', cafes: 'coffee' };

// Разовая поправка запроса (чипы уточнения, «В тепле»): меняет только этот запрос, ничего не сохраняет.
// patch = { overrides: {...}, likes: [...], avoids: [...], drop: [...], exclude: [...] };
// метка из likes уходит из avoids и наоборот; drop — метки avoids, снятые в этой серии («Убрать «Без кафе»»,
// в том числе из «Моих предпочтений» — сохранённое не меняется); exclude → exclude_poi_ids (до 9, «Ещё»).
export function mergePrefs(base, patch = {}) {
  let likes = [...(base?.likes || [])];
  let avoids = [...(base?.avoids || [])];
  for (const tag of patch.likes || []) {
    if (!LIKE_TAGS.includes(tag)) continue;
    avoids = avoids.filter((x) => x !== PREF_CONFLICT[tag]);
    if (!likes.includes(tag)) likes.push(tag);
  }
  for (const tag of patch.avoids || []) {
    if (!AVOID_TAGS.includes(tag)) continue;
    likes = likes.filter((x) => x !== PREF_CONFLICT[tag]); // «без кафе» снимает «кофе» в этом запросе
    if (!avoids.includes(tag)) avoids.push(tag);
  }
  avoids = avoids.filter((x) => !(patch.drop || []).includes(x));
  return { likes, avoids };
}

// Поправки серии складываются; «Убрать «Без кафе»» (drop) снимает метку avoids, новое «Без кафе» — возвращает
export function mergePatch(p, add) {
  const drop = [...new Set([...(p.drop || []), ...(add.drop || [])])].filter((x) => !(add.avoids || []).includes(x));
  return {
    overrides: { ...(p.overrides || {}), ...(add.overrides || {}) },
    likes: [...new Set([...(p.likes || []), ...(add.likes || [])])],
    avoids: [...new Set([...(p.avoids || []), ...(add.avoids || [])])].filter((x) => !drop.includes(x)),
    drop,
    exclude: add.exclude || p.exclude || [],
  };
}

// Решение Megah 21.10: «Без кафе» не подходит, когда ищут именно кафе — явно выбранная категория «кафе»
// (overrides.categories) или разбор текста только про кафе. Смешанный запрос о еде — не конфликт.
export function cafeOnlyQuery(answer, request) {
  const explicit = request?.overrides?.categories;
  if (explicit?.length) return explicit.includes('cafe');
  const cats = answer?.intent?.categories || [];
  return cats.length > 0 && cats.every((c) => c === 'cafe');
}

// Чип «Без кафе» под ответом: hidden — «без кафе» уже действует; disabled — запрос про одни кафе; active
export function noCafeChipState(answer, request) {
  if (request?.prefs?.avoids?.includes('cafes')) return 'hidden';
  return cafeOnlyQuery(answer, request) ? 'disabled' : 'active';
}

// Пустой ответ при включённом «без кафе» — кнопка «Убрать «Без кафе»» (разовый повтор без метки cafes)
export function offerDropNoCafe(answer, request) {
  return !answer?.cards?.length && Boolean(request?.prefs?.avoids?.includes('cafes'));
}

// Строка «почему нет кафе»: запрос про одни кафе, а «без кафе» включено (в «Моих предпочтениях» или чипом)
export function cafeAvoidedNote(answer, request) {
  return Boolean(request?.prefs?.avoids?.includes('cafes')) && cafeOnlyQuery(answer, request);
}
