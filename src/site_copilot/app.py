"""UI de Site Selection Copilot: chat con el agente + mapa de los resultados.

Uso:
    uv run streamlit run src/site_copilot/app.py

Sin API key (p. ej. en la demo pública) arranca en modo demo con respuestas grabadas;
el visitante puede poner su propia key de OpenAI para usarlo en vivo.
"""

import os

import anthropic
import folium
import openai
import streamlit as st
from streamlit_folium import st_folium

from site_copilot import demos
from site_copilot.agent import Copilot, Resultado

st.set_page_config(page_title="Site Selection Copilot", page_icon="📍", layout="wide")

ESTILOS = {
    "perfil_zona": {"color": "#2563eb", "weight": 2, "dashArray": "6 4", "fillOpacity": 0.05},
    "isocrona": {"color": "#16a34a", "weight": 2, "fillColor": "#16a34a", "fillOpacity": 0.15},
}
COLOR_CANDIDATA = "#1e3a8a"
COLOR_COMPETIDOR = "#dc2626"


# ---------------------------------------------------------------------------
# Mapa
# ---------------------------------------------------------------------------

def construir_mapa(r: Resultado | None) -> folium.Map:
    m = folium.Map(location=[20.67, -103.39], zoom_start=11, tiles="OpenStreetMap", control_scale=True)
    if r is None:
        return m
    # Encuadre: las zonas que la respuesta validó (radios, isócronas); si no hay, los puntos.
    bounds_zonas: list[tuple[float, float]] = []
    bounds_puntos: list[tuple[float, float]] = []
    zonas = folium.FeatureGroup(name="Zonas analizadas")
    candidatas = folium.FeatureGroup(name="Puntos sugeridos")
    competidores = folium.FeatureGroup(name="Competidores")
    vistos: set = set()

    for capa in r.capas:
        herramienta, tipo, datos = capa["herramienta"], capa["tipo"], capa["datos"]
        if tipo == "geojson":
            estilo = ESTILOS.get(herramienta, ESTILOS["perfil_zona"])
            etiqueta = (
                f"Isócrona {capa['entrada'].get('minutos')} min ({capa['entrada'].get('modo')})"
                if herramienta == "isocrona" else f"Radio {capa['entrada'].get('radio_m', 1000):.0f} m"
            )
            gj = folium.GeoJson(datos, style_function=lambda _, e=estilo: e, tooltip=etiqueta)
            gj.add_to(zonas)
            b = gj.get_bounds()
            bounds_zonas += [tuple(b[0]), tuple(b[1])]
        elif tipo == "puntos" and herramienta == "rankear_zonas":
            for i, p in enumerate(datos, start=1):
                folium.Marker(
                    [p["lat"], p["lon"]],
                    tooltip=f"#{i} · AGEB {p['cvegeo']}",
                    popup=f"<b>Candidata #{i}</b><br>AGEB {p['cvegeo']}<br>{p['comercios_150m']} comercios a 150 m"
                          f"<br>{p.get('competidores_radio', '?')} competidores en el radio",
                    icon=folium.DivIcon(html=(
                        f'<div style="background:{COLOR_CANDIDATA};color:#fff;border-radius:50%;width:26px;height:26px;'
                        f'display:flex;align-items:center;justify-content:center;font:600 13px sans-serif;'
                        f'border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4)">{i}</div>'
                    ), icon_size=(26, 26), icon_anchor=(13, 13)),
                ).add_to(candidatas)
                bounds_puntos.append((p["lat"], p["lon"]))
        elif tipo == "puntos" and herramienta in ("competencia_cercana", "isocrona"):
            for p in datos:
                if p["id"] in vistos:
                    continue
                vistos.add(p["id"])
                folium.CircleMarker(
                    [p["lat"], p["lon"]], radius=5, color="#fff", weight=1,
                    fill=True, fill_color=COLOR_COMPETIDOR, fill_opacity=0.9,
                    tooltip=f"{p['nombre']} · {p['actividad']}",
                ).add_to(competidores)

    for g in (zonas, competidores, candidatas):
        g.add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    if bounds := bounds_zonas or bounds_puntos:
        m.fit_bounds(bounds, padding=(20, 20))
    return m


# ---------------------------------------------------------------------------
# Estado y modo (en vivo / demo)
# ---------------------------------------------------------------------------

def key_del_entorno() -> bool:
    proveedor = os.getenv("LLM_PROVIDER", "openai")
    return bool(os.getenv("ANTHROPIC_API_KEY" if proveedor == "anthropic" else "OPENAI_API_KEY"))


ss = st.session_state
ss.setdefault("historial", [])  # [(pregunta, Resultado, es_demo)]
ss.setdefault("copilot", None)
ss.setdefault("byok", "")

with st.sidebar:
    st.title("📍 Site Selection Copilot")
    st.caption(
        "Agente que recomienda dónde abrir un negocio usando datos públicos de INEGI "
        "(DENUE, Censo 2020) y la red vial de OpenStreetMap."
    )
    if key_del_entorno():
        modo_vivo = True
        st.success("En vivo · API key del archivo .env")
    else:
        ss.byok = st.text_input(
            "Tu API key de OpenAI (opcional)", type="password", value=ss.byok,
            help="Solo se usa en esta sesión del navegador; no se guarda.",
        )
        modo_vivo = bool(ss.byok)
        if modo_vivo:
            st.success("En vivo · con tu API key")
        else:
            st.info("Modo demo: elige una pregunta de ejemplo. Pon tu key para preguntar lo que quieras.")

    st.subheader("Ejemplos")
    lista_demos = demos.listar()
    for i, d in enumerate(lista_demos):
        if st.button(d["pregunta"], key=f"demo{i}", use_container_width=True):
            ss.historial.append((d["pregunta"], d["resultado"], True))

    if st.button("Nueva conversación", type="secondary", use_container_width=True):
        ss.historial, ss.copilot = [], None
        st.rerun()
    st.caption("Datos: INEGI (DENUE; Censo de Población y Vivienda 2020; Marco Geoestadístico 2020) · Mapa y red vial © colaboradores de OpenStreetMap")


def obtener_copilot() -> Copilot:
    if ss.copilot is None:
        client = openai.OpenAI(api_key=ss.byok) if ss.byok and not key_del_entorno() else None
        ss.copilot = Copilot(provider="openai" if client else None, client=client)
    return ss.copilot


# ---------------------------------------------------------------------------
# Layout: chat | mapa
# ---------------------------------------------------------------------------

col_chat, col_mapa = st.columns([5, 6], gap="large")

with col_chat:
    chat = st.container(height=640, border=False)
    with chat:
        if not ss.historial:
            st.markdown(
                "#### ¿Dónde abro mi negocio?\n"
                "Pregunta por un giro y una zona, por ejemplo: *¿Dónde abrirías una farmacia en Zapopan, "
                "en zona de NSE medio-alto y con poca competencia?*"
            )
        for pregunta, r, es_demo in ss.historial:
            st.chat_message("user").markdown(pregunta)
            with st.chat_message("assistant"):
                st.markdown(r.texto)
                with st.expander(f"Cómo llegué a esto · {len(r.llamadas)} herramientas"):
                    for c in r.llamadas:
                        args = ", ".join(f"{k}={v!r}" for k, v in c["entrada"].items())
                        st.code(f"{c['herramienta']}({args})" + (f"\n# error: {c['error']}" if "error" in c else ""), language="python")
                    st.caption(
                        ("Respuesta grabada · " if es_demo else "")
                        + f"{r.tokens_entrada:,} tokens de entrada · {r.tokens_salida:,} de salida"
                    )

    pregunta = st.chat_input(
        "Pregunta por un giro y una zona…" if modo_vivo else "Modo demo: elige un ejemplo o pon tu API key",
        disabled=not modo_vivo,
    )
    if pregunta:
        with chat:
            st.chat_message("user").markdown(pregunta)
            with st.status("Analizando…", expanded=True) as status:
                try:
                    r = obtener_copilot().preguntar(pregunta, on_tool=lambda n, a: status.write(f"→ `{n}`"))
                except (openai.AuthenticationError, anthropic.AuthenticationError):
                    status.update(label="API key inválida", state="error")
                    st.stop()
                except (openai.RateLimitError, anthropic.RateLimitError):
                    status.update(label="Límite de uso o saldo agotado; intenta más tarde", state="error")
                    st.stop()
                except (openai.APIError, anthropic.APIError) as e:
                    status.update(label=f"Error de la API: {e}", state="error")
                    st.stop()
                status.update(label=f"Listo · {len(r.llamadas)} herramientas", state="complete", expanded=False)
        ss.historial.append((pregunta, r, False))
        st.rerun()

with col_mapa:
    ultimo = ss.historial[-1][1] if ss.historial else None
    st_folium(construir_mapa(ultimo), height=640, use_container_width=True, returned_objects=[], key=f"mapa{len(ss.historial)}")
    st.caption("🔢 puntos sugeridos · 🔴 competidores · contornos: radio analizado (azul) e isócrona (verde)")
