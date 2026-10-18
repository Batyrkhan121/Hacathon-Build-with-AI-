"""Scenario eval for Qadam (EVAL lane, T51).

Usage:
  python -m eval.run_eval --mock              # every scenario gets web/mock/answer_sample.json
  python -m eval.run_eval                     # in-process app.pipeline.recommend + real Store
  python -m eval.run_eval --no-llm            # same, but use_llm=false in every request
  python -m eval.run_eval --url http://localhost:8000
Options: --only S1,E3   --out DIR (default eval/)

Checks use only fields of app.schemas.Answer. Writes report.md + report.json.
Exit code is always 0: the summary line "EVAL <mode>: N/15" is the result.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.schemas import Answer, RecommendRequest

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = Path(__file__).resolve().parent / "scenarios.yaml"
MOCK_PATH = ROOT / "web" / "mock" / "answer_sample.json"

OPEN_CLAIMS = ("открыто сейчас", "сейчас открыт", "open now", "қазір ашық")
NIGHT_TAGS = {"late_open", "24_7"}
ACCESS_MARKERS = ("коляск", "доступн", "не подтвержд", "wheelchair", "accessib", "кедергісіз")
HOURS_MARKERS = ("нет данных", "не подтвержд", "проверьте", "no data", "not confirmed", "check",
                 "дерек жоқ", "тексеріңіз")
KK_LETTERS = set("әғқңөұүһі")
SENTENCE_END = re.compile(r"[.!?…]+(?:\s|$)")


# --------------------------------------------------------------------- outcomes


@dataclass
class Outcome:
    """What a runner got for one request."""

    answer: Answer | None = None
    rejected: bool = False  # 422 / KeyError / ValueError: invalid input handled correctly
    error: str | None = None  # crash, 5xx, invalid JSON — always a failure
    latency_ms: int = 0


@dataclass
class Result:
    id: str
    title: str
    expected_type: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    latency_ms: int = 0
    cards: int = 0
    generation: str | None = None


Runner = Callable[[RecommendRequest], Outcome]


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ----------------------------------------------------------------------- checks


def _card_text(card: Any) -> str:
    return f"{card.why} {card.what_to_do}"


def _all_text(ans: Answer) -> str:
    parts = [ans.summary, *ans.warnings]
    for c in ans.cards:
        parts += [c.title, c.why, c.what_to_do, c.open_note]
    return " ".join(parts)


def _sentences(text: str) -> int:
    return len([s for s in SENTENCE_END.split(text.strip()) if s.strip()])


def detect_script(text: str) -> str:
    """Rough language guess by alphabet: kk (Cyrillic + kk letters) / ru / en."""
    letters = [ch for ch in text.lower() if ch.isalpha()]
    if not letters:
        return "unknown"
    cyr = sum("а" <= ch <= "я" or ch == "ё" or ch in KK_LETTERS for ch in letters)
    if cyr / len(letters) < 0.5:
        return "en"
    return "kk" if any(ch in KK_LETTERS for ch in letters) else "ru"


def check_invariants(ans: Answer) -> list[str]:
    """Rules that hold for every answer, whatever the scenario."""
    fails: list[str] = []
    ranks = [c.rank for c in ans.cards]
    if len(set(ranks)) != len(ranks):
        fails.append(f"повтор rank: {ranks}")
    ids = [c.poi_id for c in ans.cards]
    if len(set(ids)) != len(ids):
        fails.append("одно место в двух карточках")
    titles = [c.title.strip().lower() for c in ans.cards]
    if len(set(titles)) != len(titles):  # 110 of 111 playgrounds are «Детская площадка»
        fails.append(f"одинаковые названия карточек: {[c.title for c in ans.cards]}")
    warnings = " ".join(ans.warnings).lower()
    for c in ans.cards:
        if c.open_status == "unknown" and c.open_confidence == "high":
            fails.append(f"{c.poi_id}: unknown с уверенностью high")
        if c.open_status == "unknown" and not any(
            m in f"{c.open_note} {warnings}".lower() for m in HOURS_MARKERS
        ):  # hours exist for only 28% of POI: the card must say so
            fails.append(f"{c.poi_id}: часы неизвестны, но нет пометки")
        if _sentences(c.why) > 2:
            fails.append(f"{c.poi_id}: why длиннее двух предложений")
        if c.visit_min_low > c.visit_min_high:
            fails.append(f"{c.poi_id}: visit_min_low > visit_min_high")
    if ans.route is not None and not set(ans.route.stop_ids) <= set(ids):
        fails.append("маршрут ведёт в места не из карточек")
    return fails


def _check_lang(ans: Answer, want: str) -> list[str]:
    fails = []
    if ans.lang != want:
        fails.append(f"lang={ans.lang}, ожидали {want}")
    if ans.cards:
        got = detect_script(" ".join(_card_text(c) for c in ans.cards))
        if got != want:
            fails.append(f"тексты карточек похожи на {got}, ожидали {want}")
    return fails


def check_expect(ans: Answer, exp: dict[str, Any]) -> list[str]:
    """Scenario-specific checks; keys are documented in eval/scenarios.yaml."""
    fails: list[str] = []
    n = len(ans.cards)
    if "min_cards" in exp and n < exp["min_cards"]:
        fails.append(f"карточек {n} < {exp['min_cards']}")
    if "max_cards" in exp and n > exp["max_cards"]:
        fails.append(f"карточек {n} > {exp['max_cards']}")
    if "max_walk_min" in exp:
        far = [f"{c.poi_id}={c.walk_min}" for c in ans.cards if c.walk_min > exp["max_walk_min"]]
        if far:
            fails.append(f"пешком > {exp['max_walk_min']} мин: {', '.join(far)}")
    if "categories_any" in exp:
        bad = [c.category for c in ans.cards if c.category not in exp["categories_any"]]
        if bad:
            fails.append(f"категории не по запросу: {bad}")
    if "categories_forbid" in exp:
        bad = [c.category for c in ans.cards if c.category in exp["categories_forbid"]]
        if bad:
            fails.append(f"запрещённые категории: {bad}")
    if "lang" in exp:
        fails += _check_lang(ans, exp["lang"])

    route = exp.get("route")
    if route == "required" and ans.route is None:
        fails.append("нет маршрута")
    if route == "none" and ans.route is not None:
        fails.append("маршрут не ожидался")
    if exp.get("route_fits_budget") and ans.route is not None and not ans.route.fits_budget:
        fails.append(f"маршрут не укладывается в бюджет ({ans.route.total_min} мин)")

    if exp.get("labels_required"):
        for c in ans.cards:
            if not c.facts_sources or not c.open_note.strip():
                fails.append(f"{c.poi_id}: нет меток источника или пометки о часах")
    if exp.get("forbid_open_claim_without_high"):
        for c in ans.cards:
            if c.open_status == "open" and c.open_confidence == "high":
                continue
            text = f"{_card_text(c)} {c.open_note}".lower()
            if any(p in text for p in OPEN_CLAIMS):
                fails.append(f"{c.poi_id}: «открыто сейчас» без open/high")
    if exp.get("warnings_required") and not ans.warnings:
        fails.append("нет предупреждений")
    if "warnings_any" in exp:
        joined = " ".join(ans.warnings).lower()
        if not any(s.lower() in joined for s in exp["warnings_any"]):
            fails.append(f"нет предупреждения с {exp['warnings_any']}")
    if exp.get("night_safe"):
        for c in ans.cards:
            if c.open_status != "open" and not NIGHT_TAGS & set(c.tags):
                fails.append(f"{c.poi_id}: ночью без подтверждения, что открыто")
    if exp.get("wheelchair_honest"):
        warned = any(m in " ".join(ans.warnings).lower() for m in ACCESS_MARKERS)
        for c in ans.cards:
            noted = any(m in f"{c.open_note} {_card_text(c)}".lower() for m in ACCESS_MARKERS)
            if "wheelchair" not in c.tags and not (warned or noted):
                fails.append(f"{c.poi_id}: доступность не подтверждена и не помечена")
    if "text_forbid" in exp:
        text = _all_text(ans).lower()
        hits = [s for s in exp["text_forbid"] if s.lower() in text]
        if hits:
            fails.append(f"в тексте запрещённое: {hits}")
    if "text_any" in exp:
        text = " ".join([ans.summary, *ans.warnings]).lower()
        if not any(s.lower() in text for s in exp["text_any"]):
            fails.append(f"в summary/warnings нет ни одного из {exp['text_any']}")
    for key, want in (exp.get("intent") or {}).items():
        got = getattr(ans.intent, key, None)
        if got != want:
            fails.append(f"intent.{key}={got!r}, ожидали {want!r}")
    for key, want in (exp.get("data_used") or {}).items():
        got = getattr(ans.data_used, key, None)
        if got != want:
            fails.append(f"data_used.{key}={got!r}, ожидали {want!r}")
    return fails


def grade(scn: dict[str, Any], out: Outcome) -> Result:
    exp = scn.get("expect") or {}
    res = Result(
        id=scn["id"],
        title=scn["title"],
        expected_type=scn.get("expected_type", ""),
        passed=False,
        latency_ms=out.latency_ms,
    )
    if out.error:
        res.failures.append(f"ошибка: {out.error}")
        return res
    if out.rejected:
        if not exp.get("reject_ok"):
            res.failures.append("запрос отклонён (422), а ожидали ответ")
        res.passed = not res.failures
        return res
    ans = out.answer
    assert ans is not None
    res.cards = len(ans.cards)
    res.generation = ans.data_used.generation
    if exp.get("reject_ok"):
        if ans.cards or not ans.warnings:
            res.failures.append("ожидали 422 или 0 карточек с понятным предупреждением")
    else:
        res.failures += check_invariants(ans)
        res.failures += check_expect(ans, exp)
    res.passed = not res.failures
    return res


# ---------------------------------------------------------------------- runners


def mock_runner() -> Runner:
    """Same canned Answer for every request: checks the eval itself, not the system."""
    raw = json.loads(MOCK_PATH.read_text(encoding="utf-8"))
    answer = Answer.model_validate(raw)

    def run(req: RecommendRequest) -> Outcome:
        return Outcome(answer=answer.model_copy(deep=True), latency_ms=answer.latency_ms)

    return run


def inprocess_runner(no_llm: bool) -> tuple[Runner | None, list[str]]:
    """app.pipeline.recommend with the real Store. Returns (runner | None, notes)."""
    notes: list[str] = []
    try:
        from app.config import get_settings
        from app.llm import LLMClient
        from app.pipeline import Deps, recommend
        from app.store import load_store

        settings = get_settings()
        store = load_store(settings)
        llm = None if no_llm or not settings.has_llm else LLMClient(settings)
    except NotImplementedError as exc:
        return None, [f"pipeline ещё не готов: {exc}"]
    except Exception as exc:  # noqa: BLE001 — setup failure is reported, not raised
        return None, [f"не удалось поднять pipeline: {type(exc).__name__}: {exc}"]
    if llm is None and not no_llm:
        notes.append("LLM недоступна (нет ключа или LLM_ENABLED=0): ответы шаблонные")
    notes.append(f"POI в Store: {len(store.pois)}; эмбеддинги: "
                 f"{'есть' if store.embeddings is not None else 'нет, лексика'}")
    deps = Deps(store=store, llm=llm, settings=settings)

    def run(req: RecommendRequest) -> Outcome:
        t0 = time.perf_counter()
        try:
            ans = recommend(req, deps)
            return Outcome(answer=ans, latency_ms=_ms(t0))
        except (KeyError, ValueError, ValidationError):
            return Outcome(rejected=True, latency_ms=_ms(t0))
        except Exception as exc:  # noqa: BLE001 — a crash is a failed scenario
            return Outcome(error=f"{type(exc).__name__}: {exc}", latency_ms=_ms(t0))

    return run, notes


def url_runner(
    base_url: str, no_llm: bool = False, timeout_s: float = 60.0, client: Any = None
) -> tuple[Runner | None, list[str]]:
    """Live API. Probes GET /api/health first: an unreachable server fails fast with one note
    instead of 16 timeouts. `client` is an httpx.Client (tests pass a MockTransport)."""
    import httpx

    http = client or httpx.Client(timeout=timeout_s)
    base = base_url.rstrip("/")
    try:
        health = http.get(f"{base}/api/health")
        health.raise_for_status()
        info = health.json()
    except (httpx.HTTPError, ValueError) as exc:
        return None, [f"API {base} недоступен: {type(exc).__name__}"]
    notes = [
        f"API: status={info.get('status')}, POI={info.get('poi_count')}, "
        f"index={info.get('index')}, llm={info.get('llm')}"
    ]
    if info.get("llm") == "off" and not no_llm:
        notes.append("LLM на сервере выключена: ответы шаблонные")

    def run(req: RecommendRequest) -> Outcome:
        body = req.model_dump(mode="json", exclude_none=True)
        t0 = time.perf_counter()
        try:
            resp = http.post(f"{base}/api/recommend", json=body)
        except httpx.HTTPError as exc:
            return Outcome(error=f"сеть: {type(exc).__name__}", latency_ms=_ms(t0))
        ms = _ms(t0)
        if resp.status_code == 422:
            return Outcome(rejected=True, latency_ms=ms)
        if resp.status_code != 200:
            return Outcome(error=f"HTTP {resp.status_code}", latency_ms=ms)
        try:
            return Outcome(answer=Answer.model_validate(resp.json()), latency_ms=ms)
        except (ValueError, ValidationError) as exc:
            return Outcome(error=f"ответ не по схеме Answer: {str(exc)[:200]}", latency_ms=ms)

    return run, notes


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


# ----------------------------------------------------------------------- report


def run_all(scenarios: list[dict[str, Any]], runner: Runner, no_llm: bool = False) -> list[Result]:
    results = []
    for scn in scenarios:
        data = dict(scn["request"])
        if no_llm:
            data["use_llm"] = False
        try:
            req = RecommendRequest.model_validate(data)
        except ValidationError as exc:
            results.append(grade(scn, Outcome(error=f"сценарий не по схеме: {exc}")))
            continue
        results.append(grade(scn, runner(req)))
    return results


def render_md(mode: str, results: list[Result], notes: list[str]) -> str:
    passed = sum(r.passed for r in results)
    lines = [
        "# Отчёт eval Qadam",
        "",
        f"- Режим: `{mode}`",
        f"- Запуск: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %z')}",
        f"- **Итог: {passed}/{len(results)}**",
    ]
    lines += [f"- {n}" for n in notes]
    lines += [
        "",
        "| id | сценарий | итог | что не прошло | карточек | генерация | latency, мс |",
        "|---|---|---|---|---:|---|---:|",
    ]
    for r in results:
        why = "; ".join(r.failures).replace("|", "/") or "—"
        lines.append(
            f"| {r.id} | {r.title} | {'pass' if r.passed else 'FAIL'} | {why} | {r.cards} "
            f"| {r.generation or '—'} | {r.latency_ms} |"
        )
    return "\n".join(lines) + "\n"


def write_report(out_dir: Path, mode: str, results: list[Result], notes: list[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(render_md(mode, results, notes), encoding="utf-8")
    payload = {
        "mode": mode,
        "passed": sum(r.passed for r in results),
        "total": len(results),
        "notes": notes,
        "results": [asdict(r) for r in results],
    }
    (out_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Qadam scenario eval")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--mock", action="store_true", help="use web/mock/answer_sample.json")
    group.add_argument("--url", help="live API base URL, e.g. http://localhost:8000")
    ap.add_argument("--no-llm", action="store_true", help="send use_llm=false in every request")
    ap.add_argument("--only", help="comma-separated scenario ids, e.g. S1,E3")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--scenarios", type=Path, default=SCENARIOS_PATH)
    args = ap.parse_args(argv)

    scenarios = load_scenarios(args.scenarios)
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        scenarios = [s for s in scenarios if s["id"] in wanted]

    notes: list[str] = []
    runner: Runner | None
    if args.mock:
        mode = "mock"
        runner = mock_runner()
        notes.append("МОК: один и тот же ответ на все запросы — проверяет сам eval, не систему")
    elif args.url:
        mode = f"url {args.url}" + (" --no-llm" if args.no_llm else "")
        runner, notes = url_runner(args.url, no_llm=args.no_llm)
    else:
        mode = "in-process" + (" --no-llm" if args.no_llm else "")
        runner, notes = inprocess_runner(args.no_llm)

    if runner is None:
        results = [grade(s, Outcome(error=notes[0])) for s in scenarios]
    else:
        results = run_all(scenarios, runner, no_llm=args.no_llm)

    write_report(args.out, mode, results, notes)
    passed = sum(r.passed for r in results)
    for r in results:
        print(f"{r.id:<4} {'pass' if r.passed else 'FAIL'}  {'; '.join(r.failures)[:160]}")
    for n in notes:
        print(f"note: {n}")
    print(f"EVAL {mode}: {passed}/{len(results)} -> {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
