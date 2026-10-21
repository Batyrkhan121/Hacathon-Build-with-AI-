// «Без кафе» и категория «кафе» в запросе, без DOM и без i18n (проверяется в node, web/tests).
// Решение 21.10 (вариант B): категорию UI не выбирает — её разбирает сервер из текста (answer.intent.categories;
// overrides.categories учитываем, если когда-нибудь появится). Сервер: явное «без кафе» сильнее разбора текста —
// кафе не ищет, даже если запрос про кафе. Конфликт — только когда запрос про одни кафе («хочу кофе»):
// у «где поесть» (кафе, ресторан, фастфуд) «Без кафе» — нормальное уточнение.

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

// Строка «почему нет кафе»: запрос про одни кафе, а «без кафе» включено (в «Моих предпочтениях» или чипом)
export function cafeAvoidedNote(answer, request) {
  return Boolean(request?.prefs?.avoids?.includes('cafes')) && cafeOnlyQuery(answer, request);
}
