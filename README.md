# Qadam: ИИ-помощник для прогулок по Астане

Qadam подсказывает, куда сходить рядом. Вы задаёте точку (ориентир или район), время и запрос, а в ответ получаете 1–3 места. Для каждого места указано, почему оно подходит, сколько времени займёт и что там делать. Можно сразу построить мини-маршрут. Каждый факт сопровождается источником и уровнем уверенности.

Проект сделан для хакатона Build with AI, кейс НИЦ Smart City.

> Статус: каркас v1. Модули пишутся параллельно по [плану](docs/PLAN.md).

## Запуск
```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt      # macOS/Linux: .venv/bin/python
cp .env.example .env                                           # впишите ANTHROPIC_API_KEY
.venv/Scripts/python -m uvicorn app.api:app --port 8000        # открыть http://localhost:8000
```

## Документы
- [Кейс](docs/TASK.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [План](docs/PLAN.md)
- [Промпты для Claude Code](docs/PROMPTS.md)
- Ещё появятся: концепция, RAG, сценарии и ограничения, этика и приватность, данные.

## Данные
© участники OpenStreetMap (ODbL), Wikidata (CC0). Погода: Open-Meteo.
