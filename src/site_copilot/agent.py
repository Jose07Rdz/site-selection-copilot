"""Agente de selección de ubicaciones: LLM + herramientas geoespaciales vía tool calling.

Soporta dos proveedores con las mismas herramientas y el mismo prompt:
    LLM_PROVIDER=openai     (Responses API, modelo por defecto gpt-5-mini)
    LLM_PROVIDER=anthropic  (Tool Runner, modelo por defecto claude-opus-5-5)

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
import openai
from anthropic import beta_tool
from anthropic.lib.tools import ToolError as SdkToolError
from dotenv import load_dotenv

from site_copilot.config import ROOT
from site_copilot.tools import geo
from site_copilot.tools.geo import SCHEMA_DOC

load_dotenv(ROOT / ".env")

DEFAULT_MODELS = {"openai": "gpt-5-mini", "anthropic": "claude-opus-5-5"}
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
2 o 3 con perfil_zona, competencia_cercana o isocrona en su punto_sugerido (no en el centroide de la AGEB). \
No recomiendes un punto que no hayas revisado.
- Cero competidores no siempre es buena señal: revisa en perfil_zona que haya actividad comercial y \
menciona si la zona parece puramente residencial.
- Usa consulta_sql solo para preguntas que las demás herramientas no cubren.
- Basa cada cifra en un resultado de herramienta. Si los datos no alcanzan para responder, dilo.
- "NSE medio-alto" equivale a nse_min=4 y "alto" a nse_min=5, salvo que el usuario indique otra cosa.

Formato de la respuesta final (en español, breve y accionable):
1. Recomendación en 1-2 frases.
2. Tabla en Markdown con las candidatas: zona/AGEB, punto sugerido (lat, lon), población en el radio, competidores, comercios cercanos, NSE.
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
        except Exception as e:  # errores inesperados: se registran y el modelo ve un mensaje genérico
            registro.llamadas.append({"herramienta": nombre, "entrada": kwargs, "error": f"{type(e).__name__}: {e}"[:300]})
            raise SdkToolError(f"Falla interna de la herramienta ({type(e).__name__}); intenta otro enfoque.") from e
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
    def rankear_zonas(
        municipios: list[str], scian: list[str], nse_min: int = 1, radio_m: float = 1000, top_n: int = 10, min_comercio: int = 25
    ) -> str:
        """Ordena las AGEB urbanas de uno o varios municipios como candidatas para abrir un negocio.

        Score = población en el radio / (competidores en el radio + 1), calculado desde el centroide de cada AGEB.
        Excluye zonas sin actividad comercial (fraccionamientos residenciales sin locales). Cada candidata trae un
        punto_sugerido: el lugar con más comercio cercano, que es donde conviene validar y ubicar el local.

        Args:
            municipios: Nombres de municipios a considerar, p. ej. ["Guadalajara", "Zapopan"].
            scian: Códigos SCIAN (2 a 6 dígitos, como prefijo) de los negocios que compiten.
            nse_min: Quintil NSE municipal mínimo de la AGEB (1 a 5).
            radio_m: Radio de influencia en metros (50 a 10000).
            top_n: Número de candidatas a devolver (máximo 30).
            min_comercio: Mínimo de comercios al por menor a 500 m (0 a 500). 25 descarta el ~10 % más residencial;
                bájalo solo si el usuario busca explícitamente zonas residenciales.
        """
        return run(
            "rankear_zonas", geo.rankear_zonas,
            municipios=municipios, scian=scian, nse_min=nse_min, radio_m=radio_m, top_n=top_n, min_comercio=min_comercio,
        )

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


class AnthropicBackend:
    """Claude vía el Tool Runner del SDK de Anthropic (el SDK maneja el loop de herramientas)."""

    def __init__(self, model: str, effort: str, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic()
        self.model, self.effort = model, effort
        self.messages: list[dict] = []

    def preguntar(self, system: str, pregunta: str, resultado: Resultado, on_tool) -> None:
        self.messages.append({"role": "user", "content": pregunta})
        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
            max_iterations=MAX_ITERATIONS,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
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
            return
        resultado.texto = "\n".join(b.text for b in ultimo.content if b.type == "text").strip()
        if ultimo.stop_reason == "max_tokens":
            resultado.texto += "\n\n[Respuesta truncada por límite de tokens]"


class OpenAIBackend:
    """Modelos de OpenAI vía la Responses API, con un loop manual de function calling."""

    def __init__(self, model: str, effort: str, client: openai.OpenAI | None = None):
        self.client = client or openai.OpenAI()
        self.model, self.effort = model, effort
        self.items: list = []  # historial en formato de la Responses API

    def _es_de_razonamiento(self) -> bool:
        return self.model.startswith(("gpt-5", "o1", "o3", "o4"))

    def preguntar(self, system: str, pregunta: str, resultado: Resultado, on_tool) -> None:
        tools = _make_tools(resultado)
        por_nombre = {t.name: t for t in tools}
        specs = [
            # strict=False: nuestros esquemas tienen parámetros opcionales con valor por defecto.
            {"type": "function", "name": d["name"], "description": d["description"], "parameters": d["input_schema"], "strict": False}
            for d in (t.to_dict() for t in tools)
        ]
        extra = {}
        if self._es_de_razonamiento():
            # Con store=False el razonamiento se reenvía cifrado en cada paso del loop.
            extra = {"reasoning": {"effort": self.effort}, "include": ["reasoning.encrypted_content"]}
        self.items.append({"role": "user", "content": pregunta})

        for _ in range(MAX_ITERATIONS):
            resp = self.client.responses.create(
                model=self.model,
                instructions=system,
                input=self.items,
                tools=specs,
                parallel_tool_calls=True,
                max_output_tokens=16000,
                store=False,
                **extra,
            )
            if resp.usage:
                resultado.tokens_entrada += resp.usage.input_tokens
                resultado.tokens_salida += resp.usage.output_tokens
            # Se devuelven todos los items de salida (incluidos los de razonamiento) para continuar el turno.
            self.items.extend(resp.output)
            llamadas = [o for o in resp.output if o.type == "function_call"]
            if not llamadas:
                resultado.texto = resp.output_text.strip()
                if resp.status == "incomplete":
                    resultado.texto += "\n\n[Respuesta incompleta: límite de tokens]"
                return
            for c in llamadas:
                try:
                    args = json.loads(c.arguments or "{}")
                    on_tool(c.name, args)
                    salida = por_nombre[c.name].call(args)
                except KeyError:
                    salida = f"ERROR: la herramienta '{c.name}' no existe."
                except json.JSONDecodeError:
                    salida = "ERROR: los argumentos no son JSON válido."
                except Exception as e:  # mismo criterio que el Tool Runner: el modelo ve el error y corrige
                    salida = f"ERROR: {e}"
                self.items.append({"type": "function_call_output", "call_id": c.call_id, "output": salida})
        resultado.texto = "Se alcanzó el máximo de pasos sin una respuesta final."


Backend = AnthropicBackend | OpenAIBackend


class Copilot:
    """Conversación con memoria: cada pregunta se agrega al historial del proveedor elegido."""

    def __init__(self, provider: str | None = None, model: str | None = None, client: Any = None):
        self.provider = (provider or os.getenv("LLM_PROVIDER", "openai")).lower()
        if self.provider not in DEFAULT_MODELS:
            raise ValueError(f"LLM_PROVIDER debe ser uno de {list(DEFAULT_MODELS)}")
        self.model = model or os.getenv("LLM_MODEL") or DEFAULT_MODELS[self.provider]
        effort = os.getenv("LLM_EFFORT", "medium")
        cls = OpenAIBackend if self.provider == "openai" else AnthropicBackend
        self.backend: Backend = cls(self.model, effort, client)
        self.system = SYSTEM_PROMPT.format(cobertura=_cobertura(), schema=SCHEMA_DOC)

    def preguntar(self, pregunta: str, on_tool: Callable[[str, dict], None] | None = None) -> Resultado:
        resultado = Resultado(texto="")
        self.backend.preguntar(self.system, pregunta, resultado, on_tool or (lambda *_: None))
        return resultado


def _imprimir_herramienta(nombre: str, entrada: dict) -> None:
    args = json.dumps(entrada, ensure_ascii=False)
    print(f"  → {nombre}({args[:140]}{'…' if len(args) > 140 else ''})", file=sys.stderr)


def main() -> None:
    try:
        copilot = Copilot()
    except (anthropic.AnthropicError, openai.OpenAIError, ValueError) as e:
        sys.exit(f"No se pudo iniciar el agente: {e}\nRevisa LLM_PROVIDER y la API key en .env (ver .env.example).")
    print(f"[{copilot.provider} · {copilot.model}]", file=sys.stderr)

    preguntas = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else None
    while True:
        pregunta = preguntas.pop(0) if preguntas else (input("\n¿Qué quieres analizar? (vacío para salir)\n> ").strip() if preguntas is None else "")
        if not pregunta:
            break
        try:
            r = copilot.preguntar(pregunta, on_tool=_imprimir_herramienta)
        except (anthropic.AuthenticationError, openai.AuthenticationError):
            sys.exit("API key inválida. Revisa tu .env.")
        except (anthropic.RateLimitError, openai.RateLimitError) as e:
            print(f"Límite de uso o saldo agotado: {e}", file=sys.stderr)
            continue
        except (anthropic.APIStatusError, openai.APIStatusError) as e:
            print(f"Error de la API ({e.status_code}): {e.message}", file=sys.stderr)
            continue
        except (anthropic.APIConnectionError, openai.APIConnectionError):
            print("Sin conexión con la API.", file=sys.stderr)
            continue
        print(f"\n{r.texto}\n")
        print(f"[{len(r.llamadas)} llamadas a herramientas · {r.tokens_entrada:,} tokens de entrada · {r.tokens_salida:,} de salida]", file=sys.stderr)


if __name__ == "__main__":
    main()
