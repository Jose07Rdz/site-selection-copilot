"""Respuestas grabadas para el modo demo (la demo pública no gasta créditos de API).

Grabar una demo (usa la API configurada en .env):
    uv run python -m site_copilot.demos --titulo "Farmacia en Zapopan" --categoria Ranking "¿Dónde abrirías una farmacia en Zapopan?"
"""

import argparse
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
CATEGORIAS = ["Ranking", "Isócrona", "Competencia", "Comparación", "Perfil", "Datos"]


def _slug(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:60]


def listar() -> list[dict]:
    demos = []
    for p in sorted(DEMOS_DIR.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        d["resultado"] = Resultado(**d["resultado"])
        demos.append(d)
    orden = {c: i for i, c in enumerate(CATEGORIAS)}
    return sorted(demos, key=lambda d: (orden.get(d.get("categoria"), len(orden)), d.get("titulo", "")))


def grabar(pregunta: str, titulo: str, categoria: str) -> Path:
    copilot = Copilot()
    r = copilot.preguntar(pregunta, on_tool=lambda n, a: print(f"  → {n}", file=sys.stderr))
    DEMOS_DIR.mkdir(exist_ok=True)
    path = DEMOS_DIR / f"{_slug(pregunta)}.json"
    data = {
        "titulo": titulo,
        "categoria": categoria,
        "pregunta": pregunta,
        "proveedor": copilot.provider,
        "modelo": copilot.model,
        "fecha": date.today().isoformat(),
        "resultado": asdict(r),
    }
    # Las salidas completas de las herramientas solo sirven para evaluar; no hacen falta en la demo.
    for c in data["resultado"]["llamadas"]:
        c.pop("salida", None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Graba una respuesta del agente como ejemplo para la app.")
    ap.add_argument("pregunta")
    ap.add_argument("--titulo", required=True, help="Título corto para la tarjeta del ejemplo")
    ap.add_argument("--categoria", required=True, help="Ranking, Isócrona, Competencia, Perfil, Comparación o Datos")
    a = ap.parse_args()
    print(f"Guardado {grabar(a.pregunta, a.titulo, a.categoria).relative_to(ROOT)}")
