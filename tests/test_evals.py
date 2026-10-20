"""Pruebas de los calificadores del set de evaluación (sin llamar a ningún LLM)."""

import json

import pytest

from evals.graders import calificar, coordenadas_ancladas, extraer_numeros, proporcion_cifras_ancladas, valor_esperado, verificar
from evals.run import CASOS, resumen_md
from site_copilot.agent import Resultado
from site_copilot.config import DB_PATH

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="Corre primero site_copilot.data.build_db")


def resultado(texto: str, llamadas: list[dict] | None = None) -> Resultado:
    return Resultado(texto=texto, llamadas=llamadas or [])


def llamada(herramienta: str, entrada: dict, salida: dict | None = None, error: str | None = None) -> dict:
    c = {"herramienta": herramienta, "entrada": entrada, "salida": json.dumps(salida or {})}
    if error:
        c["error"] = error
    return c


def test_extraer_numeros_formatos_comunes():
    assert extraer_numeros("1,476,491 hab · 4.73 por 10k · 114.9 mil · 1.5 millones") == [1476491, 4.73, 10000, 114900, 1500000]


def test_cifra_exacta_y_tolerancia():
    spec = {"sql": "SELECT poblacion FROM municipios WHERE cve_mun = '14120'"}
    ok = verificar({"tipo": "cifra", "valor": spec, "tolerancia": 0}, resultado("Zapopan tiene 1,476,491 habitantes."))
    redondeo = verificar({"tipo": "cifra", "valor": spec, "tolerancia": 0.01}, resultado("Unos 1.48 millones de habitantes."))
    mal = verificar({"tipo": "cifra", "valor": spec, "tolerancia": 0}, resultado("Zapopan tiene 1,500,000 habitantes."))
    assert ok.ok and redondeo.ok and not mal.ok


def test_porcentaje_acepta_fraccion_o_por_ciento():
    spec = {"sql": "SELECT 0.4"}
    assert verificar({"tipo": "cifra", "valor": spec, "porcentaje": True, "tolerancia": 0.03}, resultado("El 40 % tiene auto.")).ok
    assert verificar({"tipo": "cifra", "valor": spec, "porcentaje": True, "tolerancia": 0.03}, resultado("pct = 0.4")).ok


def test_scian_acepta_prefijos():
    r = resultado("", [llamada("competencia_cercana", {"scian": ["4641"]})])
    assert verificar({"tipo": "scian", "incluye": ["464111", "464112"]}, r).ok
    r = resultado("", [llamada("competencia_cercana", {"scian": ["464111"]})])
    assert not verificar({"tipo": "scian", "incluye": ["464111", "464112"]}, r).ok


def test_argumento_ignora_llamadas_con_error_y_acentos():
    r = resultado("", [
        llamada("rankear_zonas", {"municipios": ["Tonalá"], "nse_min": 3}, error="falló"),
        llamada("rankear_zonas", {"municipios": ["Tonalá"], "nse_min": 4}),
    ])
    assert verificar({"tipo": "argumento", "herramienta": "rankear_zonas", "campo": "nse_min", "igual": 4}, r).ok
    assert not verificar({"tipo": "argumento", "herramienta": "rankear_zonas", "campo": "nse_min", "igual": 3}, r).ok
    assert verificar({"tipo": "argumento", "herramienta": "rankear_zonas", "campo": "municipios", "contiene": "Tonala"}, r).ok


def test_detecta_coordenadas_inventadas():
    r = resultado(
        "Abre en 20.76789, -103.425391 o en 20.123, -103.999.",
        [llamada("rankear_zonas", {}, {"candidatas": [{"punto_sugerido": {"lat": 20.76789, "lon": -103.425391}}]})],
    )
    check = coordenadas_ancladas(r, "pregunta")
    assert check is not None and not check.ok and "20.123" in check.detalle


def test_cifras_ancladas_es_proporcion():
    r = resultado("Hay 12,776 personas y 99,999 negocios.", [llamada("perfil_zona", {}, {"poblacion_estimada": 12776})])
    assert proporcion_cifras_ancladas(r, "") == 0.5


def test_cifras_ancladas_ignora_parametros_redondos():
    r = resultado("Puedo usar un radio de 500 m o 1,500 metros; hay 12,776 personas.", [llamada("perfil_zona", {}, {"poblacion_estimada": 12776})])
    assert proporcion_cifras_ancladas(r, "") == 1.0


def test_sin_candidatas():
    vacia = resultado("", [llamada("rankear_zonas", {}, {"candidatas": []})])
    con = resultado("", [llamada("rankear_zonas", {}, {"candidatas": [{"cvegeo": "1"}]})])
    assert verificar({"tipo": "sin_candidatas"}, vacia).ok and not verificar({"tipo": "sin_candidatas"}, con).ok


def test_todos_los_casos_son_validos_y_calculables():
    casos = json.loads(CASOS.read_text(encoding="utf-8"))
    assert len(casos) >= 30 and len({c["id"] for c in casos}) == len(casos)
    for caso in casos:
        for check in caso["checks"]:
            if "valor" in check:
                assert valor_esperado(check["valor"]) is not None, caso["id"]
        calificar(caso, resultado("respuesta vacía"))  # ningún tipo de verificación desconocido


def test_reporte_resume_por_categoria():
    res = [
        {"id": "A", "categoria": "Ranking", "ok": True, "checks": [{"tipo": "scian", "ok": True}], "cifras_ancladas": 1.0,
         "herramientas": 3, "segundos": 10, "tokens_entrada": 1000, "tokens_salida": 100},
        {"id": "B", "categoria": "Datos", "ok": False, "checks": [{"tipo": "cifra", "ok": False, "descripcion": "x", "detalle": "y"}],
         "cifras_ancladas": None, "herramientas": 1, "segundos": 5, "tokens_entrada": 500, "tokens_salida": 50},
    ]
    md = resumen_md(res, "gpt-5-mini", 0.01)
    assert "1/2 casos correctos (50%)" in md and "| Datos | 0/1 | 0% |" in md and "**B** (Datos)" in md
