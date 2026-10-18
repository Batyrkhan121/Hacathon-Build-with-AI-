"""Environment smoke test for Qadam (task T00). Owner: INTEGRATOR.

Usage:
    .venv/Scripts/python scripts/check_env.py          # local checks + network
    .venv/Scripts/python scripts/check_env.py --llm    # + real Claude (and Gemini embed) calls

Prints OK / SKIP / FAIL per check and never prints secrets. Exit code 1 if something failed.
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

Result = tuple[str, str, str]  # (status, name, detail), status in OK / SKIP / FAIL


class Skip(Exception):
    """Optional check that does not apply (e.g. no GEMINI_API_KEY -> lexical retrieval)."""


def run(name: str, fn: Callable[[], str]) -> Result:
    try:
        return "OK", name, fn() or ""
    except Skip as exc:
        return "SKIP", name, str(exc)
    except Exception as exc:  # noqa: BLE001 - a smoke test reports every failure
        return "FAIL", name, f"{type(exc).__name__}: {exc}"


def check_python() -> str:
    if sys.version_info < (3, 11):  # noqa: UP036 - the point of this script is to check it
        raise RuntimeError(f"Python {platform.python_version()} < 3.11")
    return f"{platform.python_version()} ({sys.executable})"


def check_packages() -> str:
    mods = ["fastapi", "uvicorn", "pydantic", "dotenv", "httpx", "numpy", "h3", "anthropic",
            "google.genai", "yaml", "pytest"]
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
        raise RuntimeError(".env missing -> copy .env.example to .env and set ANTHROPIC_API_KEY")
    from app.config import get_settings

    s = get_settings()
    if not s.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is empty in .env")
    embed = f"{s.embed_model}/{s.embed_dim}" if s.has_embeddings else "off (lexical)"
    workspace = "set" if s.anthropic_workspace_id else "none"
    return (f"key set ({len(s.anthropic_api_key)} chars); workspace={workspace}; "
            f"model={s.claude_model}; fallback={s.claude_fallback_model}; embeddings={embed}")


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


def _claude():  # noqa: ANN202
    import anthropic

    from app.config import get_settings

    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key, timeout=s.llm_timeout_s,
                                 max_retries=1, default_headers=s.anthropic_headers)
    return client, s


def check_claude_models() -> str:
    import anthropic

    client, s = _claude()
    wanted = {"CLAUDE_MODEL": s.claude_model, "CLAUDE_FALLBACK_MODEL": s.claude_fallback_model}
    missing = {}
    for env_name, model in wanted.items():
        try:
            client.models.retrieve(model)
        except anthropic.NotFoundError:
            missing[env_name] = model
    if missing:
        available = sorted(m.id for m in client.models.list())
        raise RuntimeError(f"not available: {missing}. Available: {available}")
    return f"{s.claude_model} + {s.claude_fallback_model} available"


def check_claude_structured() -> str:
    from pydantic import BaseModel

    class Capital(BaseModel):
        city: str

    client, s = _claude()
    resp = client.messages.parse(
        model=s.claude_model,
        max_tokens=2000,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": "Столица Казахстана? Ответь JSON."}],
        output_format=Capital,
    )
    if resp.parsed_output is None:
        raise RuntimeError(f"no parsed output, stop_reason={resp.stop_reason}")
    return f"{s.claude_model}: city='{resp.parsed_output.city[:40]}'"


def check_embed() -> str:
    from app.config import get_settings

    s = get_settings()
    if not s.has_embeddings:
        raise Skip("GEMINI_API_KEY not set -> no vector index, retrieval is lexical")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=s.gemini_api_key)
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
    parser.add_argument("--llm", action="store_true",
                        help="also call Claude and the optional Gemini embeddings (uses quota)")
    args = parser.parse_args()

    checks: list[tuple[str, Callable[[], str]]] = [
        ("python >= 3.11", check_python),
        ("packages", check_packages),
        ("git", check_git),
        ("contract import", check_contract),
        (".env + ANTHROPIC_API_KEY", check_dotenv),
        ("net: Overpass API", http_check("https://overpass-api.de/api/status")),
        ("net: Wikidata", http_check(
            "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q1520"
            "&props=descriptions&languages=ru&format=json")),
        ("net: Open-Meteo", http_check(
            "https://api.open-meteo.com/v1/forecast?latitude=51.13&longitude=71.43"
            "&current=temperature_2m")),
        ("net: OSM tiles (map background)", http_check("https://tile.openstreetmap.org/0/0/0.png")),
    ]
    if args.llm:
        checks += [
            ("claude: models", check_claude_models),
            ("claude: structured output", check_claude_structured),
            ("gemini: embed (optional)", check_embed),
        ]

    results = [run(name, fn) for name, fn in checks]
    width = max(len(name) for _, name, _ in results)
    for status, name, detail in results:
        print(f"[{status}] {name.ljust(width)}  {detail}")
    failed = [name for status, name, _ in results if status == "FAIL"]
    print("\nALL OK" if not failed else f"\nFAILED: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
