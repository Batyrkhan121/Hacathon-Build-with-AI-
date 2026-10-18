"""Environment smoke test for Qadam (task T00). Owner: INTEGRATOR.

Usage:
    .venv/Scripts/python scripts/check_env.py          # local checks + network
    .venv/Scripts/python scripts/check_env.py --llm    # + real Gemini calls (uses quota)

Prints OK / FAIL per check and never prints secrets. Exit code 1 if something failed.
"""

from __future__ import annotations

import argparse
import importlib
import platform
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

Result = tuple[bool, str, str]


def run(name: str, fn: Callable[[], str]) -> Result:
    try:
        return True, name, fn() or ""
    except Exception as exc:  # noqa: BLE001 - a smoke test reports every failure
        return False, name, f"{type(exc).__name__}: {exc}"


def check_python() -> str:
    if sys.version_info < (3, 11):  # noqa: UP036 - the point of this script is to check it
        raise RuntimeError(f"Python {platform.python_version()} < 3.11")
    return f"{platform.python_version()} ({sys.executable})"


def check_packages() -> str:
    mods = ["fastapi", "uvicorn", "pydantic", "dotenv", "httpx", "numpy", "h3", "google.genai",
            "yaml", "pytest"]
    missing = []
    for mod in mods:
        try:
            importlib.import_module(mod)
        except Exception:  # noqa: BLE001
            missing.append(mod)
    if missing:
        raise RuntimeError(f"missing {missing} -> python -m pip install -r requirements.txt")
    return "all imported"


def check_git() -> str:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def check_contract() -> str:
    from app.schemas import SCHEMA_VERSION

    return f"schemas {SCHEMA_VERSION}"


def check_dotenv() -> str:
    if not (ROOT / ".env").exists():
        raise RuntimeError(".env missing -> copy .env.example to .env and set GEMINI_API_KEY")
    from app.config import get_settings

    s = get_settings()
    if not s.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is empty in .env")
    return (f"key set ({len(s.gemini_api_key)} chars); model={s.gemini_model}; "
            f"fallback={s.gemini_fallback_model}; embed={s.embed_model}/{s.embed_dim}")


def http_check(url: str) -> Callable[[], str]:
    def _check() -> str:
        import httpx

        from app.config import get_settings

        # Wikimedia answers 403 to a User-Agent without contact info (robot policy).
        ua = {"User-Agent": f"QadamHackathon/0.1 (check_env; {get_settings().contact})"}
        resp = httpx.get(url, timeout=20, headers=ua, follow_redirects=True)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return f"HTTP {resp.status_code}"

    return _check


def _client():  # noqa: ANN202
    from google import genai

    from app.config import get_settings

    return genai.Client(api_key=get_settings().gemini_api_key), get_settings()


def check_models() -> str:
    client, s = _client()
    names = {getattr(m, "name", "").removeprefix("models/") for m in client.models.list()}
    wanted = {"GEMINI_MODEL": s.gemini_model, "GEMINI_FALLBACK_MODEL": s.gemini_fallback_model,
              "EMBED_MODEL": s.embed_model}
    missing = {k: v for k, v in wanted.items() if v not in names}
    if missing:
        hint = sorted(n for n in names if "flash" in n or "embedding" in n)[:20]
        raise RuntimeError(f"not available: {missing}. Available flash/embedding: {hint}")
    return f"{len(names)} models visible; configured models available"


def check_generate() -> str:
    client, s = _client()
    resp = client.models.generate_content(
        model=s.gemini_model, contents="Ответь одним словом: столица Казахстана?"
    )
    return f"{s.gemini_model}: '{(resp.text or '').strip()[:40]}'"


def check_embed() -> str:
    from google.genai import types

    client, s = _client()
    resp = client.models.embed_content(
        model=s.embed_model,
        contents=["тихое кафе с розеткой у EXPO"],
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY", output_dimensionality=s.embed_dim
        ),
    )
    dim = len(resp.embeddings[0].values)
    if dim != s.embed_dim:
        raise RuntimeError(f"dim {dim} != EMBED_DIM {s.embed_dim}")
    return f"{s.embed_model}: dim={dim}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="also call Gemini (uses quota)")
    args = parser.parse_args()

    checks: list[tuple[str, Callable[[], str]]] = [
        ("python >= 3.11", check_python),
        ("packages", check_packages),
        ("git", check_git),
        ("contract import", check_contract),
        (".env + GEMINI_API_KEY", check_dotenv),
        ("net: Overpass API", http_check("https://overpass-api.de/api/status")),
        ("net: Wikidata", http_check(
            "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q1520"
            "&props=descriptions&languages=ru&format=json")),
        ("net: Open-Meteo", http_check(
            "https://api.open-meteo.com/v1/forecast?latitude=51.13&longitude=71.43"
            "&current=temperature_2m")),
        ("net: unpkg (Leaflet CDN)", http_check("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js")),
    ]
    if args.llm:
        checks += [
            ("gemini: models.list", check_models),
            ("gemini: generate", check_generate),
            ("gemini: embed", check_embed),
        ]

    results = [run(name, fn) for name, fn in checks]
    width = max(len(name) for _, name, _ in results)
    for ok, name, detail in results:
        print(f"[{'OK' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    failed = [name for ok, name, _ in results if not ok]
    print("\nALL OK" if not failed else f"\nFAILED: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
