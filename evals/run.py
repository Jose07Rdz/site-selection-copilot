"""Corre el set de evaluación contra el agente y genera un reporte.

Uso:
    uv run python -m evals.run                         # todos los casos, proveedor/modelo de .env
    uv run python -m evals.run --ids R1,C1,I1          # solo algunos casos
    uv run python -m evals.run --model gpt-5.4-mini --max-costo 0.50

Cada corrida gasta API: el costo estimado se imprime al final y la corrida se detiene
(sin lanzar casos nuevos) al pasar --max-costo.
"""

import argparse
import json
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from evals.graders import calificar
from site_copilot.agent import Copilot, Resultado

CASOS = Path(__file__).with_name("casos.json")
RESULTADOS = Path(__file__).with_name("resultados")

# USD por millón de tokens (entrada, salida). Aproximado: revisa la página de precios del proveedor.
PRECIOS = {
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5": (1.25, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def costo(modelo: str, tin: int, tout: int) -> float | None:
    if modelo not in PRECIOS:
        return None
    pin, pout = PRECIOS[modelo]
    return (tin * pin + tout * pout) / 1e6


def correr_caso(caso: dict, proveedor: str | None, modelo: str | None) -> dict:
    t0 = time.time()
    copilot = Copilot(provider=proveedor, model=modelo)
    try:
        r = copilot.preguntar(caso["pregunta"])
    except Exception as e:  # un error de API cuenta como caso fallido, no detiene la corrida
        return {"id": caso["id"], "categoria": caso["categoria"], "ok": False, "error": f"{type(e).__name__}: {e}",
                "segundos": round(time.time() - t0, 1), "tokens_entrada": 0, "tokens_salida": 0, "modelo": copilot.model}
    cal = calificar(caso, r)
    llamadas = [{**c, "salida": c.get("salida", "")[:1500]} for c in r.llamadas]
    return {
        "id": caso["id"], "categoria": caso["categoria"], "pregunta": caso["pregunta"], "modelo": copilot.model,
        "ok": cal.ok, "checks": [asdict(c) for c in cal.checks], "cifras_ancladas": cal.cifras_ancladas,
        "herramientas": len(r.llamadas), "errores_herramientas": sum("error" in c for c in r.llamadas),
        "tokens_entrada": r.tokens_entrada, "tokens_salida": r.tokens_salida,
        "segundos": round(time.time() - t0, 1), "respuesta": r.texto, "llamadas": llamadas,
    }


def resumen_md(res: list[dict], modelo: str, costo_total: float | None) -> str:
    n = len(res)
    ok = sum(x["ok"] for x in res)
    lineas = [
        f"# Evaluación · {modelo} · {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"**{ok}/{n} casos correctos ({ok / n:.0%})**"
        + (f" · costo estimado US${costo_total:.3f}" if costo_total is not None else ""),
        "",
        "| Categoría | Correctos | % |",
        "|---|---:|---:|",
    ]
    por_cat: dict[str, list[dict]] = defaultdict(list)
    for x in res:
        por_cat[x["categoria"]].append(x)
    for cat, xs in por_cat.items():
        k = sum(x["ok"] for x in xs)
        lineas.append(f"| {cat} | {k}/{len(xs)} | {k / len(xs):.0%} |")

    por_tipo: dict[str, list[bool]] = defaultdict(list)
    for x in res:
        for c in x.get("checks", []):
            por_tipo[c["tipo"]].append(c["ok"])
    lineas += ["", "| Verificación | Aprobadas | % |", "|---|---:|---:|"]
    for tipo, oks in sorted(por_tipo.items()):
        lineas.append(f"| {tipo} | {sum(oks)}/{len(oks)} | {sum(oks) / len(oks):.0%} |")

    anc = [x["cifras_ancladas"] for x in res if x.get("cifras_ancladas") is not None]
    if anc:
        lineas += ["", f"Cifras de la respuesta respaldadas por una herramienta (promedio): **{sum(anc) / len(anc):.0%}**"]
    prom = lambda k: sum(x.get(k, 0) for x in res) / n  # noqa: E731
    lineas += [f"Promedio por caso: {prom('herramientas'):.1f} herramientas · {prom('segundos'):.0f} s · "
               f"{prom('tokens_entrada') + prom('tokens_salida'):,.0f} tokens", ""]

    fallas = [x for x in res if not x["ok"]]
    if fallas:
        lineas += ["## Casos fallidos", ""]
        for x in fallas:
            motivo = x.get("error") or "; ".join(f"{c['descripcion']} ({c['detalle']})" for c in x["checks"] if not c["ok"])
            lineas.append(f"- **{x['id']}** ({x['categoria']}): {motivo}")
    return "\n".join(lineas) + "\n"


def recalificar(path: Path) -> None:
    casos = {c["id"]: c for c in json.loads(CASOS.read_text(encoding="utf-8"))}
    res = json.loads(path.read_text(encoding="utf-8"))
    for x in res:
        if "respuesta" not in x or x["id"] not in casos:
            continue
        cal = calificar(casos[x["id"]], Resultado(texto=x["respuesta"], llamadas=x["llamadas"]))
        x.update(ok=cal.ok, checks=[asdict(c) for c in cal.checks], cifras_ancladas=cal.cifras_ancladas)
    modelo = res[0]["modelo"] if res else "?"
    total = costo(modelo, sum(x["tokens_entrada"] for x in res), sum(x["tokens_salida"] for x in res))
    path.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    md = resumen_md(res, modelo, total).replace("# Evaluación", "# Evaluación (recalificada)", 1)
    path.with_suffix(".md").write_text(md, encoding="utf-8")
    print(md)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", help="IDs separados por coma (p. ej. R1,C1)")
    ap.add_argument("--provider", help="openai | anthropic (por defecto, LLM_PROVIDER de .env)")
    ap.add_argument("--model", help="Modelo (por defecto, LLM_MODEL de .env o el del proveedor)")
    ap.add_argument("--paralelo", type=int, default=4)
    ap.add_argument("--max-costo", type=float, default=1.0, help="USD; deja de lanzar casos al rebasarlo")
    ap.add_argument("--recalificar", metavar="RESULTADOS.json",
                    help="Vuelve a calificar respuestas guardadas con los calificadores actuales, sin llamar a la API")
    a = ap.parse_args()

    if a.recalificar:
        recalificar(Path(a.recalificar))
        return

    casos = json.loads(CASOS.read_text(encoding="utf-8"))
    if a.ids:
        ids = set(a.ids.split(","))
        casos = [c for c in casos if c["id"] in ids]
    modelo = Copilot(provider=a.provider, model=a.model).model
    if modelo not in PRECIOS:
        print(f"Aviso: no tengo precio para {modelo}; el tope de costo no aplicará.")
    print(f"Evaluando {len(casos)} casos con {modelo} (tope US${a.max_costo:.2f})…")

    res, gastado, lock = [], 0.0, threading.Lock()
    detener = threading.Event()

    def tarea(caso):
        if detener.is_set():
            return None
        return correr_caso(caso, a.provider, a.model)

    with ThreadPoolExecutor(max_workers=a.paralelo) as ex:
        futuros = [ex.submit(tarea, c) for c in casos]
        for f in as_completed(futuros):
            x = f.result()
            if x is None:
                continue
            with lock:
                res.append(x)
                gastado += costo(modelo, x["tokens_entrada"], x["tokens_salida"]) or 0
                if gastado > a.max_costo:
                    detener.set()
            estado = "✓" if x["ok"] else "✗"
            print(f"  {estado} {x['id']:3s} {x['segundos']:5.1f}s  US${gastado:.3f} acumulado")

    res.sort(key=lambda x: [c["id"] for c in casos].index(x["id"]))
    if detener.is_set():
        print(f"Se alcanzó el tope de costo: {len(res)}/{len(casos)} casos evaluados.")
    total = costo(modelo, sum(x["tokens_entrada"] for x in res), sum(x["tokens_salida"] for x in res))
    RESULTADOS.mkdir(exist_ok=True)
    base = RESULTADOS / f"{datetime.now():%Y%m%d-%H%M}_{modelo}"
    base.with_suffix(".json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    md = resumen_md(res, modelo, total)
    base.with_suffix(".md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"Detalle: {base.with_suffix('.json')}")


if __name__ == "__main__":
    main()
