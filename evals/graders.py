"""Calificadores deterministas para las respuestas del agente.

No usan otro LLM como juez: las respuestas correctas se calculan con las mismas herramientas
(o SQL) sobre la base, y cada verificación es una regla explícita. Así una corrida es barata,
reproducible y fácil de auditar.
"""

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from site_copilot.agent import Resultado
from site_copilot.tools import geo


@dataclass
class Check:
    tipo: str
    descripcion: str
    ok: bool
    detalle: str = ""


@dataclass
class Calificacion:
    checks: list[Check] = field(default_factory=list)
    cifras_ancladas: float | None = None  # proporción de cifras grandes de la respuesta que salen de una herramienta

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


# ---------------------------------------------------------------------------
# Texto y números
# ---------------------------------------------------------------------------

def norm(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode()


# 1,476,491 · 114902 · 4.73 · 114.9 mil · 10k · 1.5 millones (comas o espacios finos como separador de miles)
_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:[,  ]\d{3})+|\d+)(?:\.(\d+))?(?:\s*(mil|millones?)\b|(k)\b)?", re.I)
_COORD = re.compile(r"(-?\d{1,2}\.\d{3,})\s*,\s*(-?\d{2,3}\.\d{3,})")


# Radios y tiempos redondos ("1,000 m", "10 minutos") suelen ser parámetros del análisis, no datos.
_UNIDAD_PARAMETRO = re.compile(r"\s*(m|metros|km|kil[oó]metros|min|minutos)\b", re.I)


def extraer_numeros(texto: str, sin_parametros: bool = False) -> list[float]:
    nums = []
    for m in _NUM.finditer(texto):
        entero, dec, escala, k = m.groups()
        escala = escala or k
        v = float(re.sub(r"[,  ]", "", entero) + (f".{dec}" if dec else ""))
        if escala:
            v *= 1_000_000 if escala.lower().startswith("mill") else 1_000
        if sin_parametros and v % 50 == 0 and _UNIDAD_PARAMETRO.match(texto, m.end()):
            continue
        nums.append(v)
    return nums


def coincide(objetivo: float, candidatos: list[float], tolerancia: float) -> bool:
    margen = max(abs(objetivo) * tolerancia, 0.5 if float(objetivo).is_integer() else 0.005)
    return any(abs(c - objetivo) <= margen for c in candidatos)


# ---------------------------------------------------------------------------
# Valores esperados (calculados de la base)
# ---------------------------------------------------------------------------

def _navegar(obj: Any, ruta: str) -> Any:
    for parte in ruta.split("."):
        obj = obj[int(parte)] if isinstance(obj, list) else obj[parte]
    return obj


@cache
def _valor_cacheado(spec_json: str) -> Any:
    spec = json.loads(spec_json)
    if "sql" in spec:
        return geo.con().execute(spec["sql"]).fetchone()[0]
    if spec["funcion"] == "isocrona":
        from site_copilot.tools.isochrones import isocrona as fn
    else:
        fn = {"perfil_zona": geo.perfil_zona, "competencia_cercana": geo.competencia_cercana}[spec["funcion"]]
    return _navegar(fn(**spec["args"]), spec["campo"])


def valor_esperado(spec: dict) -> Any:
    return _valor_cacheado(json.dumps(spec, sort_keys=True))


# ---------------------------------------------------------------------------
# Verificaciones
# ---------------------------------------------------------------------------

def _llamadas_ok(r: Resultado, herramienta: str | None = None) -> list[dict]:
    return [c for c in r.llamadas if "error" not in c and (herramienta is None or c["herramienta"] == herramienta)]


def _codigos_scian(r: Resultado) -> set[str]:
    return {s for c in r.llamadas for s in (c["entrada"].get("scian") or [])}


def verificar(check: dict, r: Resultado) -> Check:
    tipo = check["tipo"]
    desc = check.get("descripcion", "")

    if tipo == "usa_herramienta":
        h = check["herramienta"]
        return Check(tipo, f"usa {h}", bool(_llamadas_ok(r, h)))

    if tipo == "no_usa_herramienta":
        h = check["herramienta"]
        return Check(tipo, f"no usa {h}", not any(c["herramienta"] == h for c in r.llamadas))

    if tipo == "scian":
        usados = _codigos_scian(r)
        faltan = [e for e in check["incluye"] if not any(e.startswith(u) for u in usados)]
        return Check(tipo, f"SCIAN {', '.join(check['incluye'])}", not faltan,
                     f"usó {sorted(usados)}" + (f"; faltan {faltan}" if faltan else ""))

    if tipo == "argumento":
        h, campo = check["herramienta"], check["campo"]
        valores = [c["entrada"].get(campo) for c in _llamadas_ok(r, h)]
        if "igual" in check:
            esperado = check["igual"]
            ok = any(v == esperado or (isinstance(v, (int, float)) and isinstance(esperado, (int, float)) and float(v) == float(esperado))
                     for v in valores)
            etiqueta = f"{h}.{campo} = {esperado!r}"
        else:
            ok = any(norm(check["contiene"]) in norm(json.dumps(v, ensure_ascii=False)) for v in valores)
            etiqueta = f"{h}.{campo} contiene {check['contiene']!r}"
        return Check(tipo, etiqueta, ok, f"valores: {valores}")

    if tipo == "cifra":
        esperado = float(valor_esperado(check["valor"]))
        nums = extraer_numeros(r.texto)
        objetivos = [esperado, esperado * 100] if check.get("porcentaje") else [esperado]
        ok = any(coincide(o, nums, check.get("tolerancia", 0)) for o in objetivos)
        return Check(tipo, desc, ok, f"esperado {esperado:,.4g}" + ("" if ok else f"; en la respuesta: {nums[:12]}"))

    if tipo == "texto_calculado":
        esperado = str(valor_esperado(check["valor"]))
        return Check(tipo, desc, norm(esperado).strip() in norm(r.texto), f"esperado {esperado!r}")

    if tipo == "texto":
        ok = bool(re.search(check["regex"], r.texto, re.I) or re.search(check["regex"], norm(r.texto), re.I))
        return Check(tipo, desc or f"menciona /{check['regex']}/", ok)

    if tipo == "texto_no":
        ok = not re.search(check["regex"], r.texto, re.I)
        return Check(tipo, desc or f"no menciona /{check['regex']}/", ok)

    if tipo == "sin_candidatas":
        candidatas = [x for c in _llamadas_ok(r, "rankear_zonas") for x in json.loads(c["salida"]).get("candidatas", [])]
        return Check(tipo, "no recomienda zonas fuera de cobertura", not candidatas, f"{len(candidatas)} candidatas")

    raise ValueError(f"Tipo de verificación desconocido: {tipo}")


def _fuentes(r: Resultado, pregunta: str) -> list[float]:
    """Todos los números que el agente vio: la pregunta, entradas y salidas de herramientas."""
    textos = [pregunta] + [json.dumps(c["entrada"]) + c.get("salida", "") for c in r.llamadas]
    return [float(x) for t in textos for x in re.findall(r"-?\d+(?:\.\d+)?", t)]


def coordenadas_ancladas(r: Resultado, pregunta: str) -> Check | None:
    """Toda coordenada que menciona la respuesta debe venir de la pregunta o de una herramienta."""
    pares = [(float(a), float(b)) for a, b in _COORD.findall(r.texto)]
    if not pares:
        return None
    fuentes = _fuentes(r, pregunta)
    inventadas = [p for p in pares if not all(any(abs(x - f) < 0.0015 for f in fuentes) for x in p)]
    return Check("coordenadas_ancladas", "coordenadas salen de herramientas", not inventadas,
                 f"{len(pares)} coordenadas" + (f"; sin respaldo: {inventadas[:3]}" if inventadas else ""))


def proporcion_cifras_ancladas(r: Resultado, pregunta: str) -> float | None:
    """Métrica (no pasa/falla): cifras >= 100 de la respuesta que aparecen en datos de herramientas (±1 %).

    No cuenta años ni radios/tiempos redondos (p. ej. "500 m"), que son parámetros y no datos.
    """
    cifras = [n for n in extraer_numeros(r.texto, sin_parametros=True) if n >= 100 and not (1990 <= n <= 2030 and n.is_integer())]
    if not cifras:
        return None
    fuentes = [abs(f) for f in _fuentes(r, pregunta)]
    return sum(coincide(n, fuentes, 0.01) for n in cifras) / len(cifras)


def calificar(caso: dict, r: Resultado) -> Calificacion:
    cal = Calificacion(checks=[verificar(c, r) for c in caso["checks"]])
    if (coords := coordenadas_ancladas(r, caso["pregunta"])) is not None:
        cal.checks.append(coords)
    cal.cifras_ancladas = proporcion_cifras_ancladas(r, caso["pregunta"])
    return cal
