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
- `GEMINI_API_KEY` по желанию, для смыслового поиска по эмбеддингам. Без него поиск лексический.

Потом запускайте так же, как во втором способе. Проверить окружение и ключи можно командой `.venv/Scripts/python scripts/check_env.py --llm`. Файл `.env` не коммитьте.

## Документы
- [Кейс](docs/TASK.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [План](docs/PLAN.md)
- [Промпты для Claude Code](docs/PROMPTS.md)
- [Концепция](docs/CONCEPT.md), [RAG](docs/RAG.md), [сценарии](docs/SCENARIOS.md)
- [Этика и приватность](docs/ETHICS.md), [данные](docs/DATA.md)
- [Питч](docs/PITCH.md), [сценарий видео](docs/VIDEO.md)
- [Исправления Meyrban и проверка перед сдачей 18 октября до 23:59](docs/RELEASE.md)

## Проверка перед сдачей

Из корня проекта, без ключей и платных запросов (для UI-тестов нужен Node.js 22+):

```bash
.venv/Scripts/python -m pytest -ra
.venv/Scripts/python -m ruff check app pipeline eval scripts tests
node --test tests/frontend_state.test.mjs tests/frontend_render.test.mjs
.venv/Scripts/python -m eval.run_eval --no-llm --strict --out work/eval
.venv/Scripts/python -m eval.run_eval --no-llm --strict --scenarios eval/probes.yaml --out work/probes
```

`--strict` возвращает ненулевой код при провале сценария. Эти проверки также запускаются
GitHub Actions для pull request. Реальную генерацию с ключом Claude нужно отдельно проверить
на машине показа; успешные тесты с подменённым провайдером не подтверждают доступность API.

## Данные
© участники OpenStreetMap (ODbL), Wikidata (CC0). Погода: Open-Meteo.
