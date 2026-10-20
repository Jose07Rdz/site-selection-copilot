"""Respuestas grabadas para el modo demo (la demo pública no gasta créditos de API).

Grabar una demo (usa la API configurada en .env):
    uv run python -m site_copilot.demos "¿Dónde abrirías una farmacia en Zapopan?"
"""

import json
import re
import sys
import unicodedata
from dataclasses import asdict
from datetime import date
from pathlib import Path

from site_copilot.agent import Copilot, Resultado
from site_copilot.config import ROOT

DEMOS_DIR = ROOT / "demos"


def _slug(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:60]


def listar() -> list[dict]:
    demos = []
    for p in sorted(DEMOS_DIR.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        d["resultado"] = Resultado(**d["resultado"])
        demos.append(d)
    return demos


def grabar(pregunta: str) -> Path:
    copilot = Copilot()
    r = copilot.preguntar(pregunta, on_tool=lambda n, a: print(f"  → {n}", file=sys.stderr))
    DEMOS_DIR.mkdir(exist_ok=True)
    path = DEMOS_DIR / f"{_slug(pregunta)}.json"
    data = {
        "pregunta": pregunta,
        "proveedor": copilot.provider,
        "modelo": copilot.model,
        "fecha": date.today().isoformat(),
        "resultado": asdict(r),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('Uso: python -m site_copilot.demos "pregunta"')
    print(f"Guardado {grabar(' '.join(sys.argv[1:])).relative_to(ROOT)}")
