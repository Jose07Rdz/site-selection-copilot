"""La app en modo demo (sin API key) no debe llamar a ninguna API y debe mostrar los ejemplos grabados."""

import pytest
from streamlit.testing.v1 import AppTest

from site_copilot.config import DB_PATH, ROOT
from site_copilot.demos import listar

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="Corre primero site_copilot.data.build_db")
APP = str(ROOT / "src" / "site_copilot" / "app.py")


@pytest.fixture
def sin_keys(monkeypatch):
    # Variables vacías: load_dotenv no sobrescribe las ya definidas, así que el .env local no cuenta.
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(var, "")


def test_modo_demo_sin_key(sin_keys):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert at.chat_input[0].disabled
    assert any("Modo demo" in i.value for i in at.info)


def test_ejemplo_grabado_se_muestra(sin_keys):
    demos = listar()
    assert demos, "No hay demos grabadas en demos/"
    at = AppTest.from_file(APP, default_timeout=60).run()
    boton = next(b for b in at.button if b.label == demos[0]["pregunta"])
    at = boton.click().run()
    assert not at.exception
    textos = [m.markdown[0].value for m in at.chat_message if m.markdown]
    assert demos[0]["pregunta"] in textos
    assert demos[0]["resultado"].texto in textos
