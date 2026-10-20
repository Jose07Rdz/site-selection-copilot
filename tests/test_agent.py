"""Prueba el loop del agente con respuestas simuladas de la API (sin red ni API key)."""

import json

import anthropic
import httpx
import httpx2
import openai
import pytest

from site_copilot.agent import Copilot
from site_copilot.config import DB_PATH

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="Corre primero site_copilot.data.build_db")


def _msg(content, stop_reason):
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def _client_simulado(respuestas: list[dict], peticiones: list[dict]) -> anthropic.Anthropic:
    def handler(request: httpx2.Request) -> httpx2.Response:
        peticiones.append(json.loads(request.content))
        return httpx2.Response(200, json=respuestas[len(peticiones) - 1])

    return anthropic.Anthropic(api_key="test", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))


def test_loop_ejecuta_herramientas_y_conserva_historial():
    peticiones: list[dict] = []
    respuestas = [
        _msg([
            {"type": "tool_use", "id": "toolu_1", "name": "buscar_actividad", "input": {"texto": "farmacia"}},
            {"type": "tool_use", "id": "toolu_2", "name": "perfil_zona", "input": {"lat": 20.695, "lon": -103.387, "radio_m": 500}},
        ], "tool_use"),
        _msg([{"type": "text", "text": "Recomiendo Providencia."}], "end_turn"),
    ]
    copilot = Copilot(provider="anthropic", client=_client_simulado(respuestas, peticiones))
    r = copilot.preguntar("¿Dónde abro una farmacia?")

    assert r.texto == "Recomiendo Providencia."
    assert [c["herramienta"] for c in r.llamadas] == ["buscar_actividad", "perfil_zona"]
    # El círculo del perfil va a las capas del mapa, no al modelo.
    assert [c["tipo"] for c in r.capas] == ["geojson"]

    primera, segunda = peticiones
    assert primera["model"] == "claude-opus-5-5"
    assert primera["fallbacks"] == "default"
    assert {t["name"] for t in primera["tools"]} >= {"rankear_zonas", "isocrona", "consulta_sql"}

    # Ambos resultados regresan juntos en un solo mensaje de usuario.
    resultados = segunda["messages"][-1]["content"]
    assert [b["tool_use_id"] for b in resultados] == ["toolu_1", "toolu_2"]
    assert "Farmacias sin minisúper" in json.dumps(resultados, ensure_ascii=False)
    assert "_geojson" not in json.dumps(resultados)

    # Historial: user, assistant(tool_use), user(tool_result), assistant(texto).
    assert [m["role"] for m in copilot.backend.messages] == ["user", "assistant", "user", "assistant"]


def test_error_de_herramienta_regresa_como_is_error():
    peticiones: list[dict] = []
    respuestas = [
        _msg([{"type": "tool_use", "id": "toolu_1", "name": "perfil_zona", "input": {"lat": -103.387, "lon": 20.695}}], "tool_use"),
        _msg([{"type": "text", "text": "Corregí las coordenadas."}], "end_turn"),
    ]
    copilot = Copilot(provider="anthropic", client=_client_simulado(respuestas, peticiones))
    copilot.preguntar("Perfil de Providencia")

    resultado = peticiones[1]["messages"][-1]["content"][0]
    assert resultado["is_error"] is True
    assert "Invertiste lat y lon" in json.dumps(resultado, ensure_ascii=False)


# --- OpenAI (Responses API) -------------------------------------------------

def _resp(output, status="completed"):
    return {
        "id": "resp_test", "object": "response", "created_at": 0, "model": "gpt-5-mini", "status": status,
        "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 5}},
    }


def _fcall(call_id, name, args):
    return {"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id, "name": name,
            "arguments": json.dumps(args), "status": "completed"}


def _texto(t):
    return {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": t, "annotations": []}]}


def _openai_simulado(respuestas: list[dict], peticiones: list[dict]) -> openai.OpenAI:
    def handler(request: httpx.Request) -> httpx.Response:
        peticiones.append(json.loads(request.content))
        return httpx.Response(200, json=respuestas[len(peticiones) - 1])

    return openai.OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_openai_loop_ejecuta_herramientas_y_conserva_historial():
    peticiones: list[dict] = []
    respuestas = [
        _resp([
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "abc"},
            _fcall("call_1", "buscar_actividad", {"texto": "farmacia"}),
            _fcall("call_2", "perfil_zona", {"lat": 20.695, "lon": -103.387, "radio_m": 500}),
        ]),
        _resp([_texto("Recomiendo Providencia.")]),
    ]
    copilot = Copilot(provider="openai", client=_openai_simulado(respuestas, peticiones))
    r = copilot.preguntar("¿Dónde abro una farmacia?")

    assert r.texto == "Recomiendo Providencia."
    assert [c["herramienta"] for c in r.llamadas] == ["buscar_actividad", "perfil_zona"]
    assert r.tokens_entrada == 200 and r.tokens_salida == 40

    primera, segunda = peticiones
    assert primera["model"] == "gpt-5-mini" and primera["store"] is False
    assert primera["include"] == ["reasoning.encrypted_content"]
    assert {t["name"] for t in primera["tools"]} >= {"rankear_zonas", "isocrona", "consulta_sql"}

    # El segundo paso reenvía razonamiento + llamadas + sus resultados, en orden.
    tipos = [i.get("type", i.get("role")) for i in segunda["input"]]
    assert tipos == ["user", "reasoning", "function_call", "function_call", "function_call_output", "function_call_output"]
    salidas = [i for i in segunda["input"] if i.get("type") == "function_call_output"]
    assert [o["call_id"] for o in salidas] == ["call_1", "call_2"]
    assert "Farmacias sin minisúper" in salidas[0]["output"]
    assert "_geojson" not in json.dumps(segunda)


def test_openai_error_de_herramienta_llega_al_modelo():
    peticiones: list[dict] = []
    respuestas = [
        _resp([_fcall("call_1", "perfil_zona", {"lat": -103.387, "lon": 20.695})]),
        _resp([_texto("Corregí las coordenadas.")]),
    ]
    copilot = Copilot(provider="openai", client=_openai_simulado(respuestas, peticiones))
    copilot.preguntar("Perfil de Providencia")

    salida = peticiones[1]["input"][-1]
    assert salida["type"] == "function_call_output" and salida["output"].startswith("ERROR:")
    assert "Invertiste lat y lon" in salida["output"]
