"""Agente de selección de ubicaciones: Claude + herramientas geoespaciales vía tool calling.

Uso:
    uv run site-copilot "¿Dónde abrirías una farmacia en Zapopan en zona de NSE alto?"
    uv run site-copilot            # modo conversación
"""

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic
from anthropic import beta_tool
from anthropic.lib.tools import ToolError as SdkToolError
from dotenv import load_dotenv

from site_copilot.tools import geo
from site_copilot.tools.geo import SCHEMA_DOC

MODEL = os.getenv("SITE_COPILOT_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("SITE_COPILOT_EFFORT", "high")
MAX_ITERATIONS = 15

SYSTEM_PROMPT = """\
Eres Site Selection Copilot, un analista de inteligencia de ubicación para México. Ayudas a \
decidir dónde abrir un negocio usando datos públicos de INEGI a través de tus herramientas.

Datos disponibles (cobertura actual: {cobertura}):
- DENUE: negocios con coordenadas, actividad SCIAN y personal ocupado.
- Censo de Población y Vivienda 2020 por AGEB urbana: población, escolaridad y equipamiento de vivienda.
- Índice NSE aproximado por AGEB (z-scores de escolaridad y % de viviendas con auto, internet y PC). \
Es relativo, no es el NSE AMAI. Quintil municipal: 1 = más bajo, 5 = más alto dentro del municipio.
- Red vial de OpenStreetMap para isócronas a pie y en auto.

{schema}

Cómo trabajar:
- Traduce el giro del usuario a códigos SCIAN con buscar_actividad antes de medir competencia; \
elige los códigos que de verdad compiten (p. ej., farmacias con y sin minisúper) y dilo.
- Para recomendar ubicaciones, usa rankear_zonas para generar candidatas y luego valida las mejores \
2 o 3 con perfil_zona, competencia_cercana o isocrona. No recomiendes un punto que no hayas revisado.
- Usa consulta_sql solo para preguntas que las demás herramientas no cubren.
- Basa cada cifra en un resultado de herramienta. Si los datos no alcanzan para responder, dilo.
- "NSE medio-alto" equivale a nse_min=4 y "alto" a nse_min=5, salvo que el usuario indique otra cosa.

Formato de la respuesta final (en español, breve y accionable):
1. Recomendación en 1-2 frases.
2. Tabla con las ubicaciones candidatas: zona/AGEB, coordenadas, población en el radio, competidores, NSE.
3. Por qué (2-4 viñetas con cifras).
4. Riesgos y supuestos (población 2020, DENUE no distingue cadenas de independientes, NSE aproximado).
"""


@dataclass
class Resultado:
    texto: str
    capas: list[dict[str, Any]] = field(default_factory=list)  # geometrías para el mapa
    llamadas: list[dict[str, Any]] = field(default_factory=list)  # registro de herramientas usadas
    tokens_entrada: int = 0
    tokens_salida: int = 0


def _cobertura() -> str:
    filas = geo.rows("SELECT entidad, COUNT(*) AS municipios FROM municipios GROUP BY 1 ORDER BY 1")
    return ", ".join(f"{r['entidad']} ({r['municipios']} municipios)" for r in filas)


def _make_tools(registro: Resultado) -> list:
    """Envuelve las funciones de geo como herramientas. Las llaves "_..." van al mapa, no al modelo."""

    def run(nombre: str, fn: Callable[..., dict], **kwargs) -> str:
        try:
            out = fn(**kwargs)
        except geo.ToolError as e:
            registro.llamadas.append({"herramienta": nombre, "entrada": kwargs, "error": str(e)})
            raise SdkToolError(str(e)) from e
        for k in [k for k in out if k.startswith("_")]:
            registro.capas.append({"herramienta": nombre, "tipo": k[1:], "entrada": kwargs, "datos": out.pop(k)})
        registro.llamadas.append({"herramienta": nombre, "entrada": kwargs})
        return json.dumps(out, ensure_ascii=False, default=str)

    @beta_tool
    def buscar_actividad(texto: str) -> str:
        """Busca actividades económicas del catálogo SCIAN por palabra clave y devuelve sus códigos y cuántos negocios hay.

        Args:
            texto: Palabra o frase del giro, p. ej. "farmacia", "gimnasio", "café".
        """
        return run("buscar_actividad", geo.buscar_actividad, texto=texto)

    @beta_tool
    def buscar_municipio(texto: str) -> str:
        """Busca municipios por nombre (tolera acentos y nombres parciales) y devuelve clave, población y área.

        Args:
            texto: Nombre o parte del nombre del municipio.
        """
        return run("buscar_municipio", geo.buscar_municipio, texto=texto)

    @beta_tool
    def rankear_zonas(municipios: list[str], scian: list[str], nse_min: int = 1, radio_m: float = 1000, top_n: int = 10) -> str:
        """Ordena las AGEB urbanas de uno o varios municipios como candidatas para abrir un negocio.

        Score = población en el radio / (competidores en el radio + 1), calculado desde el centroide de cada AGEB.
        Devuelve coordenadas, población, competidores, distancia al competidor más cercano y NSE.

        Args:
            municipios: Nombres de municipios a considerar, p. ej. ["Guadalajara", "Zapopan"].
            scian: Códigos SCIAN (2 a 6 dígitos, como prefijo) de los negocios que compiten.
            nse_min: Quintil NSE municipal mínimo de la AGEB (1 a 5).
            radio_m: Radio de influencia en metros (50 a 10000).
            top_n: Número de candidatas a devolver (máximo 30).
        """
        return run("rankear_zonas", geo.rankear_zonas, municipios=municipios, scian=scian, nse_min=nse_min, radio_m=radio_m, top_n=top_n)

    @beta_tool
    def perfil_zona(lat: float, lon: float, radio_m: float = 1000) -> str:
        """Perfil de un círculo alrededor de un punto: población estimada, viviendas, escolaridad, % con auto, NSE y actividades más comunes.

        Args:
            lat: Latitud en grados decimales.
            lon: Longitud en grados decimales (negativa en México).
            radio_m: Radio en metros (50 a 10000).
        """
        return run("perfil_zona", geo.perfil_zona, lat=lat, lon=lon, radio_m=radio_m)

    @beta_tool
    def competencia_cercana(lat: float, lon: float, scian: list[str], radio_m: float = 1000) -> str:
        """Cuenta los competidores (por códigos SCIAN) en un radio y lista los más cercanos con su distancia.

        Args:
            lat: Latitud en grados decimales.
            lon: Longitud en grados decimales.
            scian: Códigos SCIAN (2 a 6 dígitos, como prefijo) de los competidores.
            radio_m: Radio en metros (50 a 10000).
        """
        return run("competencia_cercana", geo.competencia_cercana, lat=lat, lon=lon, scian=scian, radio_m=radio_m)

    @beta_tool
    def isocrona(lat: float, lon: float, minutos: float, modo: str = "auto", scian: list[str] | None = None) -> str:
        """Calcula el área alcanzable en N minutos por la red vial (OpenStreetMap) y su perfil demográfico y comercial.

        Si se dan códigos SCIAN, también cuenta los competidores dentro del área. Puede tardar si la red no está en caché.

        Args:
            lat: Latitud del origen.
            lon: Longitud del origen.
            minutos: Tiempo de viaje (auto: 1 a 20, pie: 1 a 30).
            modo: "auto" o "pie".
            scian: Códigos SCIAN opcionales de los competidores.
        """
        from site_copilot.tools.isochrones import isocrona as _isocrona  # import diferido: OSMnx es pesado

        return run("isocrona", _isocrona, lat=lat, lon=lon, minutos=minutos, modo=modo, scian=scian)

    @beta_tool
    def consulta_sql(sql: str) -> str:
        """Ejecuta una consulta SQL de solo lectura (SELECT/WITH, DuckDB spatial) sobre las tablas municipios, agebs y negocios. Máximo 200 filas.

        Args:
            sql: Una sola sentencia SELECT o WITH.
        """
        return run("consulta_sql", geo.consulta_sql, sql=sql)

    return [buscar_actividad, buscar_municipio, rankear_zonas, perfil_zona, competencia_cercana, isocrona, consulta_sql]


class Copilot:
    """Conversación con memoria: cada pregunta se agrega al historial."""

    def __init__(self, client: anthropic.Anthropic | None = None):
        load_dotenv()
        self.client = client or anthropic.Anthropic()
        self.system = SYSTEM_PROMPT.format(cobertura=_cobertura(), schema=SCHEMA_DOC)
        self.messages: list[dict] = []

    def preguntar(self, pregunta: str, on_tool: Callable[[str, dict], None] | None = None) -> Resultado:
        resultado = Resultado(texto="")
        self.messages.append({"role": "user", "content": pregunta})
        runner = self.client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=16000,
            max_iterations=MAX_ITERATIONS,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            # Si el modelo rechaza la solicitud por un clasificador de seguridad, la API la reintenta con otro modelo.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            tools=_make_tools(resultado),
            messages=list(self.messages),
        )
        ultimo = None
        for message in runner:
            ultimo = message
            resultado.tokens_entrada += message.usage.input_tokens + (message.usage.cache_read_input_tokens or 0)
            resultado.tokens_salida += message.usage.output_tokens
            # El runner guarda su propia copia del historial; lo reflejamos para conservar la conversación.
            self.messages.append({"role": "assistant", "content": message.content})
            if on_tool:
                for b in message.content:
                    if b.type == "tool_use":
                        on_tool(b.name, b.input)
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                self.messages.append(tool_response)

        if ultimo is None:
            raise RuntimeError("El modelo no devolvió respuesta.")
        if ultimo.stop_reason == "refusal":
            resultado.texto = "El modelo no pudo responder esta solicitud."
        else:
            resultado.texto = "\n".join(b.text for b in ultimo.content if b.type == "text").strip()
            if ultimo.stop_reason == "max_tokens":
                resultado.texto += "\n\n[Respuesta truncada por límite de tokens]"
        return resultado


def _imprimir_herramienta(nombre: str, entrada: dict) -> None:
    args = json.dumps(entrada, ensure_ascii=False)
    print(f"  → {nombre}({args[:140]}{'…' if len(args) > 140 else ''})", file=sys.stderr)


def main() -> None:
    try:
        copilot = Copilot()
    except anthropic.AnthropicError as e:
        sys.exit(f"No se pudo crear el cliente de Anthropic: {e}\nDefine ANTHROPIC_API_KEY en .env (ver .env.example).")

    preguntas = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else None
    while True:
        pregunta = preguntas.pop(0) if preguntas else (input("\n¿Qué quieres analizar? (vacío para salir)\n> ").strip() if preguntas is None else "")
        if not pregunta:
            break
        try:
            r = copilot.preguntar(pregunta, on_tool=_imprimir_herramienta)
        except anthropic.AuthenticationError:
            sys.exit("API key inválida. Revisa ANTHROPIC_API_KEY en .env.")
        except anthropic.RateLimitError:
            print("Límite de uso alcanzado; espera un momento e intenta de nuevo.", file=sys.stderr)
            continue
        except anthropic.APIStatusError as e:
            print(f"Error de la API ({e.status_code}): {e.message}", file=sys.stderr)
            continue
        except anthropic.APIConnectionError:
            print("Sin conexión con la API de Anthropic.", file=sys.stderr)
            continue
        print(f"\n{r.texto}\n")
        print(f"[{len(r.llamadas)} llamadas a herramientas · {r.tokens_entrada:,} tokens de entrada · {r.tokens_salida:,} de salida]", file=sys.stderr)


if __name__ == "__main__":
    main()
