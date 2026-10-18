# Qadam: ИИ-помощник для прогулок по Астане

Qadam подсказывает, куда сходить рядом. Вы задаёте точку (ориентир или район), время и запрос, а в ответ получаете 1–3 места. Для каждого места указано, почему оно подходит, сколько времени займёт и что там делать. Можно сразу построить мини-маршрут. Каждый факт сопровождается источником и уровнем уверенности.

Проект сделан для хакатона Build with AI, кейс НИЦ Smart City.

> Статус: модули влиты в main, идёт доводка. Ход работ: [план](docs/PLAN.md).

## Запуск

Сначала создайте окружение (один раз):

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt      # macOS/Linux: .venv/bin/python
```

В PowerShell пути те же, но с обратным слэшем: `.venv\Scripts\python`. На macOS и Linux вместо `.venv/Scripts/python` пишите `.venv/bin/python`.

### 1. Только интерфейс на моке (без ключей и без сервера API)

Из корня репозитория:

```bash
.venv/Scripts/python -m http.server 5500                       # macOS/Linux: .venv/bin/python
```

Откройте http://localhost:5500/web/?mock=1. Ответы берутся из `web/mock/answer_sample.json`, сеть и ключи не нужны.

### 2. Всё приложение без ключей

Файл `.env` не нужен, а если он пуст, всё равно работает. Без ключа Claude ответ строится по правилам и шаблонам: разбор запроса правилами, карточки из шаблонов, поиск лексический.

```bash
.venv/Scripts/python -m uvicorn app.api:app --reload --port 8000   # macOS/Linux: .venv/bin/python
```

Откройте http://localhost:8000.

### 3. С ключами

```bash
cp .env.example .env
```

В `.env` впишите:
- `ANTHROPIC_API_KEY` для текстов Claude (разбор запроса и «почему / что делать»);
- `OPENROUTER_API_KEY` по желанию, для смыслового поиска по эмбеддингам (модель google/gemini-embedding-001 через OpenRouter). Без него поиск лексический. Индекс в `data/index` собран через OpenRouter, поэтому оставьте `EMBED_PROVIDER=openrouter`, как в `.env.example`.

Потом запускайте так же, как во втором способе. Проверить окружение и ключи можно командой `.venv/Scripts/python scripts/check_env.py --llm`. Файл `.env` не коммитьте.

## Документы
- [Кейс](docs/TASK.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [План](docs/PLAN.md)
- [Промпты для Claude Code](docs/PROMPTS.md)
- Ещё появятся: концепция, RAG, сценарии и ограничения, этика и приватность, данные.

## Данные
© участники OpenStreetMap (ODbL), Wikidata (CC0). Погода: Open-Meteo.
