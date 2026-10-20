"""UI de Site Selection Copilot: galería de análisis + mapa, y chat en vivo opcional.

Uso:
    uv run streamlit run src/site_copilot/app.py

Modos:
- Público (SITE_COPILOT_PUBLICO=1, p. ej. en Streamlit Cloud): solo los ejemplos grabados; no hay chat
  ni campo para API key, así que visitar la demo no cuesta nada.
- Local con API key en .env: además de los ejemplos, chat en vivo con el agente.
- Local sin key: ejemplos + campo opcional para usar una key propia.
"""

import base64
import importlib
import os

import anthropic
import folium
import openai
import streamlit as st
from streamlit_folium import st_folium

from site_copilot import demos
from site_copilot.agent import Copilot, Resultado

# Tras un push, Streamlit Cloud vuelve a ejecutar este archivo pero conserva los módulos ya importados.
# Recargar `demos` evita que la app nueva use la versión vieja (p. ej. sin el campo "slug").
importlib.reload(demos)

st.set_page_config(page_title="Site Selection Copilot", page_icon=":material/location_on:", layout="wide")

REPO_URL = "https://github.com/Jose07Rdz/site-selection-copilot"
LINKEDIN_URL = "https://www.linkedin.com/in/joseggrdz"

# Tokens del sistema de diseño (ver .streamlit/config.toml).
PRIMARIO = "#1E40AF"
ACENTO = "#D97706"
PELIGRO = "#DC2626"
TEXTO_SUAVE = "#475569"
BORDE = "#DBE3EF"

ESTILOS_ZONA = {
    "perfil_zona": {"color": PRIMARIO, "weight": 2, "dashArray": "6 4", "fillColor": PRIMARIO, "fillOpacity": 0.06},
    "isocrona": {"color": ACENTO, "weight": 2.5, "fillColor": ACENTO, "fillOpacity": 0.14},
}

# Ícono "map-pin" de Lucide. st.html elimina <svg> en línea, así que va como imagen.
_SVG_PIN = (
    f'<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" '
    f'stroke="{PRIMARIO}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0"/>'
    '<circle cx="12" cy="10" r="3"/></svg>'
)
ICONO_PIN = f'<img src="data:image/svg+xml;base64,{base64.b64encode(_SVG_PIN.encode()).decode()}" width="32" height="32" alt="">'

st.html(f"""
<style>
  .sc-header {{ display:flex; align-items:center; justify-content:space-between; gap:24px; flex-wrap:wrap;
               padding:4px 0 8px; border-bottom:1px solid {BORDE}; margin-bottom:4px; }}
  .sc-brand {{ display:flex; align-items:center; gap:12px; color:{PRIMARIO}; }}
  .sc-brand h1 {{ font-size:1.55rem; margin:0; padding:0; color:#0F172A; letter-spacing:-0.01em; }}
  .sc-brand p {{ margin:2px 0 0; color:{TEXTO_SUAVE}; font-size:0.95rem; max-width:640px; line-height:1.45; }}
  .sc-stack {{ display:flex; gap:6px; flex-wrap:wrap; align-items:center; }}
  .sc-chip {{ font:500 0.78rem 'Fira Code', monospace; color:{PRIMARIO}; background:#E9EEF6;
             border:1px solid {BORDE}; border-radius:999px; padding:3px 10px; white-space:nowrap; }}
  .sc-link {{ font-weight:600; color:{PRIMARIO}; text-decoration:none; padding:4px 2px; }}
  .sc-link:hover {{ text-decoration:underline; }}
  .sc-link:focus-visible {{ outline:2px solid {PRIMARIO}; outline-offset:2px; border-radius:4px; }}
  .sc-pregunta {{ border-left:4px solid {ACENTO}; background:#FFFBEB; padding:10px 14px; border-radius:6px;
                 color:#0F172A; font-size:1rem; line-height:1.5; }}
  .sc-label {{ font:600 0.75rem 'Fira Sans', sans-serif; text-transform:uppercase; letter-spacing:0.06em;
              color:{TEXTO_SUAVE}; margin:0 0 6px; }}
  .sc-legend {{ display:flex; gap:18px; flex-wrap:wrap; font-size:0.85rem; color:{TEXTO_SUAVE}; margin-top:6px; }}
  .sc-legend span {{ display:inline-flex; align-items:center; gap:6px; }}
  .sc-sw {{ display:inline-block; width:14px; height:14px; border-radius:50%; }}
  .sc-footer {{ color:{TEXTO_SUAVE}; font-size:0.8rem; border-top:1px solid {BORDE}; padding-top:10px; margin-top:8px; }}
  /* Tarjetas de la galería: texto completo (sin "…"), misma altura y alineadas a la izquierda. */
  [class*="st-key-demo"] button {{ min-height:68px; align-items:flex-start; justify-content:flex-start;
                                   text-align:left; padding:10px 12px; transition:border-color .15s, box-shadow .15s; }}
  [class*="st-key-demo"] button p {{ white-space:normal; overflow:visible; text-overflow:clip; line-height:1.3; }}
  [class*="st-key-demo"] button:hover {{ box-shadow:0 2px 8px rgba(30,64,175,.12); }}
  [class*="st-key-demo"] button > div, [class*="st-key-demo"] button [data-testid="stMarkdownContainer"] {{
      width:100%; justify-content:flex-start; text-align:left; }}
  /* Galería: tarjetas en escritorio, selector desplegable en pantallas angostas. */
  .st-key-galeria-movil {{ display:none; }}
  @media (max-width: 640px) {{
    .st-key-galeria-tarjetas {{ display:none; }}
    .st-key-galeria-movil {{ display:block; }}
  }}
  @media (prefers-reduced-motion: reduce) {{ * {{ transition:none !important; animation:none !important; }} }}
</style>
""")


# ---------------------------------------------------------------------------
# Mapa
# ---------------------------------------------------------------------------

def _marcador_numerado(i: int) -> folium.DivIcon:
    return folium.DivIcon(
        html=(
            f'<div style="background:{PRIMARIO};color:#fff;border-radius:50%;width:28px;height:28px;'
            f'display:flex;align-items:center;justify-content:center;font:600 13px Fira Sans,sans-serif;'
            f'border:2px solid #fff;box-shadow:0 1px 4px rgba(15,23,42,.45)">{i}</div>'
        ),
        icon_size=(28, 28), icon_anchor=(14, 14),
    )


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
            estilo = ESTILOS_ZONA.get(herramienta, ESTILOS_ZONA["perfil_zona"])
            etiqueta = (
                f"Isócrona: {capa['entrada'].get('minutos'):.0f} min ({capa['entrada'].get('modo')})"
                if herramienta == "isocrona" else f"Radio analizado: {capa['entrada'].get('radio_m', 1000):.0f} m"
            )
            gj = folium.GeoJson(datos, style_function=lambda _, e=estilo: e, tooltip=etiqueta)
            gj.add_to(zonas)
            b = gj.get_bounds()
            bounds_zonas += [tuple(b[0]), tuple(b[1])]
        elif tipo == "puntos" and herramienta == "rankear_zonas":
            for i, p in enumerate(datos, start=1):
                folium.Marker(
                    [p["lat"], p["lon"]],
                    tooltip=f"Candidata #{i} · AGEB {p['cvegeo']}",
                    popup=folium.Popup(
                        f"<b>Candidata #{i}</b><br>AGEB {p['cvegeo']}<br>{p['comercios_150m']} comercios a 150 m"
                        f"<br>{p.get('competidores_radio', '?')} competidores en el radio", max_width=220,
                    ),
                    icon=_marcador_numerado(i),
                ).add_to(candidatas)
                bounds_puntos.append((p["lat"], p["lon"]))
        elif tipo == "puntos" and herramienta in ("competencia_cercana", "isocrona"):
            for p in datos:
                if p["id"] in vistos:
                    continue
                vistos.add(p["id"])
                folium.CircleMarker(
                    [p["lat"], p["lon"]], radius=5, color="#fff", weight=1,
                    fill=True, fill_color=PELIGRO, fill_opacity=0.9,
                    tooltip=f"{p['nombre']} · {p['actividad']}",
                ).add_to(competidores)

    for g in (zonas, competidores, candidatas):
        g.add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    if bounds := bounds_zonas or bounds_puntos:
        m.fit_bounds(bounds, padding=(24, 24))
    return m


def metricas(r: Resultado) -> dict[str, str]:
    zonas = sum(1 for c in r.capas if c["tipo"] == "geojson")
    ids = {p["id"] for c in r.capas if c["tipo"] == "puntos" and c["herramienta"] != "rankear_zonas" for p in c["datos"]}
    candidatas = sum(len(c["datos"]) for c in r.capas if c["herramienta"] == "rankear_zonas" and c["tipo"] == "puntos")
    return {
        "Herramientas usadas": f"{len(r.llamadas)}",
        "Zonas analizadas": f"{zonas}",
        "Candidatas": f"{candidatas}" if candidatas else "—",
        "Competidores en mapa": f"{len(ids):,}" if ids else "—",
    }


# ---------------------------------------------------------------------------
# Estado y modo
# ---------------------------------------------------------------------------

PUBLICO = os.getenv("SITE_COPILOT_PUBLICO", "").lower() in ("1", "true", "si", "sí")


def key_del_entorno() -> bool:
    proveedor = os.getenv("LLM_PROVIDER", "openai")
    return bool(os.getenv("ANTHROPIC_API_KEY" if proveedor == "anthropic" else "OPENAI_API_KEY"))


ss = st.session_state
lista_demos = demos.listar()
if "seleccion" not in ss:
    # Enlace directo a un ejemplo: ?ejemplo=<slug> (p. ej. ?ejemplo=cafeteria-en-la-colonia-americana)
    pedido = next((d for d in lista_demos if d["slug"] == st.query_params.get("ejemplo")), None)
    inicial = pedido or (lista_demos[0] if lista_demos else None)
    ss.seleccion = {**inicial, "es_demo": True} if inicial else None
ss.setdefault("copilot", None)
ss.setdefault("byok", "")


def obtener_copilot() -> Copilot:
    if ss.copilot is None:
        client = openai.OpenAI(api_key=ss.byok) if ss.byok and not key_del_entorno() else None
        ss.copilot = Copilot(provider="openai" if client else None, client=client)
    return ss.copilot


# ---------------------------------------------------------------------------
# Encabezado
# ---------------------------------------------------------------------------

chips = "".join(f'<span class="sc-chip">{t}</span>' for t in ("LLM tool calling", "DuckDB spatial", "OSMnx", "INEGI"))
st.html(f"""
<header class="sc-header">
  <div class="sc-brand">
    {ICONO_PIN}
    <div>
      <h1>Site Selection Copilot</h1>
      <p>Agente de IA que recomienda dónde abrir un negocio: traduce la pregunta a consultas geoespaciales
         sobre datos públicos de INEGI y la red vial de OpenStreetMap, y justifica cada cifra.</p>
    </div>
  </div>
  <div class="sc-stack">{chips}<a class="sc-link" href="{REPO_URL}" target="_blank" rel="noopener">Código en GitHub ↗</a></div>
</header>
""")

# ---------------------------------------------------------------------------
# Galería de ejemplos
# ---------------------------------------------------------------------------

st.markdown('<p class="sc-label">Análisis de ejemplo · elige uno</p>', unsafe_allow_html=True)
presentes = [c for c in demos.CATEGORIAS if any(d.get("categoria") == c for d in lista_demos)]
filtro = st.pills("Filtrar por tipo de análisis", ["Todos", *presentes], default="Todos", label_visibility="collapsed")
visibles = [d for d in lista_demos if filtro in (None, "Todos") or d.get("categoria") == filtro]

por_fila = 5
with st.container(key="galeria-tarjetas"):
    for inicio in range(0, len(visibles), por_fila):
        cols = st.columns(por_fila)
        for col, d in zip(cols, visibles[inicio:inicio + por_fila]):
            activo = ss.seleccion is not None and ss.seleccion.get("pregunta") == d["pregunta"]
            with col:
                if st.button(
                    f"**{d.get('titulo', d['pregunta'][:40])}**  \n{d.get('categoria', '')}",
                    key=f"demo{lista_demos.index(d)}", type="primary" if activo else "secondary", use_container_width=True,
                ):
                    ss.seleccion = {**d, "es_demo": True}
                    st.query_params["ejemplo"] = d["slug"]
                    st.rerun()

with st.container(key="galeria-movil"):
    titulos = [d.get("titulo", d["pregunta"][:40]) for d in visibles]
    actual = next((i for i, d in enumerate(visibles) if ss.seleccion and d["pregunta"] == ss.seleccion.get("pregunta")), None)
    elegido = st.selectbox("Análisis de ejemplo", titulos, index=actual, placeholder="Elige un análisis…",
                           label_visibility="collapsed")
    if elegido is not None and elegido != (titulos[actual] if actual is not None else None):
        ss.seleccion = {**visibles[titulos.index(elegido)], "es_demo": True}
        st.query_params["ejemplo"] = ss.seleccion["slug"]
        st.rerun()

# ---------------------------------------------------------------------------
# Pregunta en vivo (solo fuera del modo público)
# ---------------------------------------------------------------------------

modo_vivo = False
if not PUBLICO:
    if key_del_entorno():
        modo_vivo = True
    else:
        with st.expander("Usar el agente en vivo con tu propia API key de OpenAI"):
            ss.byok = st.text_input(
                "API key de OpenAI", type="password", value=ss.byok,
                help="Solo se usa en esta sesión del navegador; no se guarda en ningún lado.",
            )
        modo_vivo = bool(ss.byok)

if modo_vivo:
    # Dentro de un contenedor el chat_input queda en línea (no fijo abajo ni con auto-scroll al final).
    with st.container():
        pregunta = st.chat_input("Haz tu propia pregunta: un giro y una zona de Jalisco…")
    if pregunta:
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
        copilot = obtener_copilot()
        ss.seleccion = {"titulo": "Tu pregunta", "pregunta": pregunta, "resultado": r, "es_demo": False,
                        "modelo": copilot.model, "proveedor": copilot.provider}
        st.query_params.pop("ejemplo", None)
        st.rerun()

st.space("small")

# ---------------------------------------------------------------------------
# Resultado: respuesta | mapa
# ---------------------------------------------------------------------------

sel = ss.seleccion
if sel is None:
    st.info("Aún no hay ejemplos grabados. Graba uno con: `uv run python -m site_copilot.demos --help`")
    st.stop()
r: Resultado = sel["resultado"]

col_texto, col_mapa = st.columns([5, 6], gap="large")

with col_texto:
    st.markdown('<p class="sc-label">Pregunta</p>', unsafe_allow_html=True)
    st.html(f'<div class="sc-pregunta">{sel["pregunta"]}</div>')
    st.markdown('<p class="sc-label" style="margin-top:14px">Respuesta del agente</p>', unsafe_allow_html=True)
    with st.container(height=520, border=True):
        st.markdown(r.texto)
    with st.expander(f"Cómo llegó a esto · {len(r.llamadas)} llamadas a herramientas"):
        for i, c in enumerate(r.llamadas, start=1):
            args = ", ".join(f"{k}={v!r}" for k, v in c["entrada"].items())
            st.code(f"{i}. {c['herramienta']}({args})" + (f"\n# error: {c['error']}" if "error" in c else ""), language="python")
    detalle = [f"Modelo: {sel.get('modelo', '—')}", f"{r.tokens_entrada + r.tokens_salida:,} tokens"]
    if sel.get("es_demo"):
        detalle.insert(0, f"Respuesta grabada el {sel.get('fecha', '')}")
    st.caption(" · ".join(detalle))

with col_mapa:
    for col, (nombre, valor) in zip(st.columns(4), metricas(r).items()):
        col.metric(nombre, valor)
    st_folium(construir_mapa(r), height=560, use_container_width=True, returned_objects=[], key=f"mapa-{sel['pregunta']}")
    st.html(f"""
    <div class="sc-legend" role="list" aria-label="Leyenda del mapa">
      <span role="listitem"><span class="sc-sw" style="background:{PRIMARIO}"></span>Punto sugerido (numerado)</span>
      <span role="listitem"><span class="sc-sw" style="background:{PELIGRO}"></span>Competidor</span>
      <span role="listitem"><span class="sc-sw" style="border:2px dashed {PRIMARIO}"></span>Radio analizado</span>
      <span role="listitem"><span class="sc-sw" style="background:{ACENTO};opacity:.6"></span>Isócrona</span>
    </div>
    """)

nota_publica = (
    " Esta versión pública muestra análisis grabados para que visitarla no tenga costo; "
    "el agente en vivo se corre localmente con tu propia API key."
    if PUBLICO else ""
)
st.html(f"""
<div class="sc-footer">
  Datos: INEGI (DENUE; Censo de Población y Vivienda 2020; Marco Geoestadístico 2020) · Mapa y red vial ©
  colaboradores de OpenStreetMap. El índice socioeconómico es una aproximación relativa, no el NSE de AMAI.{nota_publica}
  <br>Hecho por José González ·
  <a class="sc-link" href="{LINKEDIN_URL}" target="_blank" rel="noopener">LinkedIn</a> ·
  <a class="sc-link" href="{REPO_URL}" target="_blank" rel="noopener">GitHub</a>
</div>
""")
