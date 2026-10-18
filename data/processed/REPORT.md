# Данные Qadam: отчёт сборки

Сформирован: 2026-10-18 14:59 (UTC+5). Источник: OpenStreetMap (© участники OSM, лицензия ODbL), выгрузка через Overpass API. Файл генерирует `python -m pipeline.report`.

## Выгрузка
- Кусков выгрузки: 5 (zone_airport_nqz, zone_akorda_pyramid_hazrat, zone_baiterek_khan_shatyr, zone_expo_mega, zone_opera_nurly_zhol).
- Сырых элементов: 1197; после нормализации и дедупликации: 531 POI.
- Время выгрузки: 2026-10-18 14:45 … 2026-10-18 14:57 (UTC+5).
- Зеркала Overpass: https://maps.mail.ru/osm/tools/overpass/api/interpreter (3), https://overpass.kumi.systems/api/interpreter (2).
- Покрытие: только перечисленные куски (демо-зоны), без плиток всего города.

## Категории
| Категория | POI | с часами работы |
|---|---:|---:|
| cafe | 128 | 36% |
| playground | 111 | 0% |
| restaurant | 96 | 38% |
| bar | 47 | 62% |
| fast_food | 44 | 57% |
| park | 23 | 0% |
| monument | 19 | 0% |
| attraction | 14 | 7% |
| mall | 13 | 31% |
| sport | 9 | 11% |
| cinema | 7 | 71% |
| museum | 4 | 0% |
| religious | 4 | 0% |
| theatre | 4 | 0% |
| gallery | 3 | 0% |
| nightlife | 2 | 50% |
| library | 2 | 0% |
| entertainment | 1 | 100% |

## Полнота полей
- `opening_hours`: 28%.
- `wheelchair` указан: 1%.
- адрес: 16%; сайт или телефон: 23%.
- тег `wikidata` в OSM: 2% (13).
- описание: нет — 100% (531).
- название на 2+ языках: 10%.
- средний `data_confidence`: 0.39
- `rating` пуст у всех записей: настоящих рейтингов в открытых данных нет. `popularity` — документированный прокси (wiki-ссылки, полнота тегов, тип, бренд).

## Плотность (H3 r7, ≈5 км² на ячейку)
- Ячеек с данными: 17; медиана POI на ячейку: 23; ячеек с ≤3 POI: 5.

| Ячейка (центр) | Рядом | POI | с часами | кафе/еда |
|---|---|---:|---:|---:|
| 51.127, 71.406 | ТРЦ «Хан Шатыр» | 137 | 25% | 66 |
| 51.114, 71.438 | Монумент «Байтерек» | 79 | 24% | 45 |
| 51.138, 71.440 | Монумент «Байтерек» | 62 | 32% | 47 |
| 51.151, 71.408 | Театр «Астана Опера» | 60 | 30% | 49 |
| 51.125, 71.472 | Мечеть «Хазрет Султан» | 57 | 25% | 36 |
| 51.149, 71.474 | — | 28 | 46% | 25 |
| 51.079, 71.402 | ТРЦ Mega Silk Way | 25 | 56% | 14 |
| 51.090, 71.436 | EXPO (Нур Алем) | 24 | 17% | 7 |

Вывод для слайда «ограничения»: больше всего данных рядом с «ТРЦ «Хан Шатыр»» (137 POI на ≈5 км²); в 5 из 17 ячеек не больше 3 POI. Часы работы известны у 28% мест — у остальных карточка пишет «нет данных о часах, проверьте перед визитом».

## Обогащение и индекс
- Векторный индекс не построен (нет `GEMINI_API_KEY` или квоты). Retrieval работает лексически по названиям, категориям и тегам.

## Примеры записей
```json
{"id": "osm:way/460703779", "name": "Хан Шатыр", "name_i18n": {"ru": "Хан Шатыр", "en": "Khan Shatyr"}, "category": "mall", "tags": ["indoor", "late_open", "shopping", "wheelchair"], "geo": {"lat": 51.1325089, "lon": 71.4038726}, "h3_r9": "8921538228fffff", "address": "Тұран даңғылы, 37", "opening_hours": "Mo-Su,PH 10:00-23:00", "wheelchair": "yes", "indoor": true, "popularity": 0.6, "sources": ["osm"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
{"id": "osm:way/762780007", "name": "Abu Dhabi Plaza", "name_i18n": {}, "category": "mall", "tags": ["indoor", "shopping"], "geo": {"lat": 51.1219718, "lon": 71.4279322}, "h3_r9": "89215380483ffff", "address": "Сығанақ көшесі, 60/5", "opening_hours": "Mo-Su 10:00-22:00", "phone": "+7 7172 621616", "website": "https://abudhabiplaza.info/", "wheelchair": "unknown", "indoor": true, "popularity": 0.6, "sources": ["osm"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
{"id": "osm:node/11162304905", "name": "Атлас", "name_i18n": {"ru": "Атлас", "en": "Atlas"}, "category": "restaurant", "tags": ["coffee", "food", "indoor", "late_open", "vegetarian", "wifi"], "geo": {"lat": 51.131615, "lon": 71.4275325}, "h3_r9": "89215382353ffff", "address": "Қонаев көшесі, 29", "opening_hours": "Su-Sa 10:00-02:00", "phone": "+7 (771) 231-36-14", "website": "https://atlasrestobar.taplink.ws/", "wheelchair": "unknown", "indoor": true, "popularity": 0.15, "sources": ["osm"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
{"id": "osm:node/6698615689", "name": "Restobar D51", "name_i18n": {"ru": "Restobar D51", "en": "Restobar D51"}, "category": "bar", "tags": ["indoor", "late_open", "night", "wifi"], "geo": {"lat": 51.1476868, "lon": 71.4156581}, "h3_r9": "89215382077ffff", "address": "Қорғалжын тас жолы, 2", "opening_hours": "Mo-Su 16:00-04:00", "phone": "+77775552060", "wheelchair": "unknown", "indoor": true, "popularity": 0.13, "sources": ["osm"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
{"id": "osm:way/166267459", "name": "Ледовый дворец Алау", "name_i18n": {"ru": "Ледовый дворец Алау", "kk": "Алау мұзайдыны сарайы", "en": "Ice Palace Alau"}, "category": "sport", "tags": ["indoor", "sport"], "geo": {"lat": 51.1048776, "lon": 71.4050693}, "h3_r9": "89215383103ffff", "address": "Қабанбай Батыр даңғылы, 47", "phone": "+7 7172 706-722", "website": "https://alau.info/", "wheelchair": "unknown", "indoor": true, "popularity": 0.57, "sources": ["osm"], "data_confidence": 0.7, "fetched_at": "2026-10-18"}
```
