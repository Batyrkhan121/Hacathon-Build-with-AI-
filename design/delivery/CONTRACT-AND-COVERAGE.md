# Qadam · контракт и покрытие макетов

Основание: инструкция Батырхана от 18.10.2026; проверены текущие `app/schemas.py`, `app/validate.py`, `web/render.js`, `web/api.js`, `web/app.js`, `web/map.js`, `web/i18n.js` и `web/mock/answer_sample.json`. Это передача дизайнерских материалов, не изменение кода `app/**` или `web/**`.

## Поля и визуальные правила

| Что нужно показать | Существующий контракт / логика | Мок и правило макета |
|---|---|---|
| 1–3 места, короткий итог | `Answer.summary`, `Answer.cards` ≤ 3 | `localmockAnswer`; итог и начало первой карточки видны после перехода к результату |
| Номер ↔ метка на карте | `Card.rank`, `poi_id`, `geo` | Один номер на карточке, карте и таймлайне; selection — состояние UI, не поле API |
| Название и безымянный объект | `title`, `category`; `render.js:isUnnamed` | `unnamed_playground`: «Балалар алаңы · 120 м», ниже `card.unnamed` |
| Факты отдельно от текстов | `distance_m`, `walk_min`, `visit_min_low/high`, `address` | Не пересчитывать в макете; `why` и `what_to_do` — отдельные короткие абзацы |
| Источник идеи | `idea_source = poi_data / generated` | POI — нейтральный контур; ИИ — фиолетовый. Это происхождение идеи, не гарантия достоверности фактов |
| Часы и уверенность | `open_status`, `open_confidence`, `open_note` | Зеленый только `open + high`; uncertain — контур с иконкой и словом; отсутствие данных — серый |
| До закрытия / открытия | Только текст `Card.open_note` | `closes_in_min` и `opens_in_min` есть в `HoursVerdict`, **не в Card**. Макет не выводит их сам из часов |
| Источник и кесте под бейджем | `open_note`, `facts_sources` | В Card нет отдельных `opening_hours`, `hours_url`, `hours_checked_at`: показывать серверную строку; не сочинять provenance |
| Полнота данных | `data_confidence` × 100 | Подпись «Деректер толықтығы»; не называть точностью или вероятностью открытия |
| Теги | `tags` — словарь `TAG_VOCAB` | В карточке 2–4; не превращать наличие тега в обещание безопасности или доступности |
| «Картада», «Бағыт», «Сайт» | Первые два — callbacks; `Card.website` optional | **website уже присутствует** в текущем Card и заполняется в validate.py. Оригинальный мок его не содержит. Нет URL — нет кнопки |
| Свойства, отсутствующие в ответе | Нет photo/review/favorite/booking полей | Не переносить из прежнего Explore фотографии, рейтинги, избранное, бронь и другие новые функции |
| Мини-маршрут | `Answer.route`: `legs`, `stop_ids`, `total_walk_m`, `total_min`, `fits_budget`, `path`, `note` | Таймлайн — отображение готового ответа; пунктирные прямые отрезки, приблизительная длина, максимум 3 остановки |
| Приватность | `RecommendRequest.location` принимает ровно `h3_cell` **или** `landmark_id` | Точные пользовательские координаты в request не добавлять. `Answer.context.origin/geo` — центр ячейки/ориентир/POI; не путать с отправкой GPS |
| Время и «машина времени» | `RecommendRequest.at`, `context.local_time`, `data_used.time` | Всегда явная дата/UTC+5 для fixture, не выдавать за сейчас |
| Полная прозрачность | `data_used`, `intent`, `latency_ms`; snapshot из `GET api/meta`; payload запроса | Details по умолчанию закрыт. `data_used.model` может быть null; snapshot не входит в Answer |
| Без ИИ | `data_used.generation=template`, `parser=rules`, `model=null`; запрос `use_llm=false` | `answers.no_ai`; метка находится над ответом, а не в новом поле Card |
| Предупреждения | `Answer.warnings[]` над карточками; локальные замечания — `open_note`/текст | В Card нет `warnings[]` и структурированного списка отсутствующих удобств; «нет данных: розетки» не выводить как новый факт |
| Языки | `lang=kk/ru/en`, реальные ключи `i18n.js` | UI по ключам, fixture-тексты ответа — KK. Смена языка UI сама не переводит серверный `why` |

## Пять карточек и выделение

`fixtures.json` содержит полные Card, а метаданные вынесены за пределы контракта:

| Ключ | Что проверяет |
|---|---|
| `cardVariants.open_poi.card` | `open+high`, нейтральный POI-бейдж, опциональный сайт |
| `cardVariants.likely_open_ai.card` | `open+medium`, outline, «ЖИ идеясы» |
| `cardVariants.unnamed_playground.card` | Без имени, низкая полнота, нет часов, нет сайта |
| `cardVariants.closing_soon.card` | `open+high`, «40 минут қалды» внутри серверной строки |
| `cardVariants.no_ai.card` | Короткий текст, используется в `answers.no_ai` с generation=template |
| `selectedVariant.uiState.selectedRank` | Выделение карточки после выбора метки; не мутирует Card |

Все карточки — **визуальные fixture, не актуальные рекомендации**. `facts_sources=["mock"]`. Высокая уверенность первого и четвертого варианта задана специально для показа компонента. Адреса и расписания вымышлены/не подтверждены. `https://fixture.invalid/alem-ai` — зарезервированный нерабочий домен для демонстрации кнопки, не реальный сайт музея.

## Покрытие состояний

| Состояние | Уже существующий путь | Fixture / способ показать |
|---|---|---|
| Первый вход | `renderIntro` + example callback | `uiStates.intro` |
| Загрузка и отмена | `renderLoading`, 3 skeleton; `cancelRequest` восстанавливает прежний экран | `uiStates.loading`; оригинальный `?mock=slow` дает задержку 3 с |
| Успешный ответ | `renderAnswer` | `localmockAnswer` |
| Ответ без ИИ | generation=template | `answers.no_ai` |
| Предупреждение / вылет | `Answer.warnings` | `answers.departure_warning`; время поездки явно fixture, не реальный расчет |
| Пусто | 0 cards, route=null | `answers.empty`; оригинальный `?mock=empty` |
| Условия изменились | `setStale`; `stale` + «Жаңарту» | `uiStates.stale`; приглушение старых карточек — требование оформления |
| Сеть / timeout / server | `ApiError`, 25 с; `renderError` | `uiStates.network_error`, `.timeout`; `?mock=error` имитирует сбой |
| 413 | `ApiError('too_large')`, сокращение запроса | `uiStates.too_large`; «без ИИ» здесь не помогает и не предлагается |
| 503 | `ApiError('unavailable')` | `uiStates.server_loading`; повторить, без ложного «все работает офлайн» |
| Пустой запрос | Проверка перед fetch, `query.empty` под полем | `uiStates.empty_query` |
| Геолокация: отказ / вне Астаны | `web/app.js` соответствующие подсказки | `uiStates.geolocation_denied`, `.outside_astana`; выбрать ориентир/карту |
| Выбор точки | `startPick`, H3 в браузере, banner + cancel | `uiStates.pick_on_map` |
| Карта недоступна | Fallback если нет Leaflet | `uiStates.map_unavailable`; карточки продолжают работать |
| Мок-режим | `web/api.js` читает `mock` query param | Видимая `app.mock` в шапке; fixtures-файл сам по себе production-мок не заменяет |
| Вне бюджета | `route.fits_budget=false` | `answers.over_budget` |

## REQUEST для POLISH / интегратора

1. **Дата демо.** 18.10.2026 — воскресенье, «завтра» = понедельник 19.10.2026. Инструкция одновременно говорит о закрытом в понедельник музее и просит показать его открытым завтра. Для положительного примера фиксировать **вторник 20.10.2026**, для понедельника показывать закрыто/исключение из рекомендаций по фактическому ответу. В fixtures заморожено 20.10.2026 17:30 UTC+5.
2. **Текущий sample не доказывает зеленое «Ашық».** Все открытые места `web/mock/answer_sample.json` имеют confidence=medium, первое и третье — POI, второе generated+unknown. Для требуемых пяти видов нужны отдельные визуальные fixture, а не незаметное повышение уверенности исходного мока.
3. **Closed+medium.** `hoursBadge` сейчас выдает уверенное «Жабық» при closed+medium; «Жабық болуы мүмкін» используется только для low. Привести к таблице брифа и различить fill/outline; в delivery есть `answers.closed_medium` для сверки.
4. **Unknown.** `hoursBadge` всегда пишет «Жұмыс уақыты расталмаған». Отсутствие данных раскрывается в `open_note`. Для отличия «нет расписания» от «расписание не удалось разобрать» не придумывать новое поле; использовать предоставленный текст или REQUEST на структурированный признак, если он станет необходим.
5. **Фиксированный OSM в подписи.** Строки `hours.open*` содержат «(OSM)» независимо от `facts_sources`. Для manual/mock это неверная атрибуция. Нужна нейтральная подпись и источник из фактических данных/`open_note`, без подмены confidence.
6. **Скоро закроется / когда откроется.** Card не передает `HoursVerdict.closes_in_min/opens_in_min`; `validate.py` оставляет только note. Если live note не содержит нужной фразы, дизайнер ее не вычисляет. Изменение контракта или серверного текста — интегратору.
7. **Казахский по умолчанию.** `detectInitial()` пока выбирает URL → saved → browser → ru. Бриф требует URL → saved → kk; исправляет владелец `web/**`, `html.lang` уже обновляется в `applyTranslations`.
8. **Оригинальный мок статичен.** `mockRecommend` не меняет язык, часы, дату, parser/generation и карточки по новому body. Даже повтор `use_llm=false` в `?mock=error` возвращает тот же sample с generation=llm. Поэтому этот режим не подтверждает реальную работу fallback/локализации. Delivery-preview использует явный `answers.no_ai`, не утверждает, что production исправлен.
9. **Сбой плиток.** Нет `tileerror`-обработчика; fallback сейчас покрывает отсутствие Leaflet, а не гарантированно недоступные OSM-плитки. Отдельный макет состояния нужен; подключение обработки — POLISH. Подложка остается стандартной OSM, attribution виден, CSS-фильтры/перекраска не добавляются.
10. **Порядок блока.** `renderAnswer` сейчас ставит мини-маршрут перед карточками, а карточка — facts/address перед badges. В брифе карточки предшествуют маршруту, badge предшествует facts. Это изменение отображения/порядка, не контракта. Верхняя часть первой карточки не должна вытесняться таймлайном.

## Проверка fixture

Ключи API не расширялись: все данные каждого `card` и каждого Answer сохраняют поля текущих моделей. `uiStates`, `assertions`, `fixtureOnly` — исключительно обертка документации и не должны попадать в `POST /api/recommend` или Answer. Состояния транспортной ошибки вообще не являются Answer.

**PASS:** 5 полных Card (точное совпадение полей модели), 7 Answer и 3 RecommendRequest проверены через Pydantic текущего `app/schemas.py`. Также проверены `TAG_VOCAB`, тексты why/what до 180 знаков, mock-provenance, фиксированная дата и суммы маршрутов. Для `answers.walk_short` отдельно подтверждены 49 минут = 3 + 6 минут пешком + 25 + 15 минут посещения и 457 м. Проверка fixture не подтверждает фактические часы/погоду/местоположение и не заменяет проверку live API.
