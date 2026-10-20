"""La app sin API key o en modo público no debe poder llamar a ninguna API y debe mostrar los ejemplos grabados."""

import pytest
from streamlit.testing.v1 import AppTest

from site_copilot.config import ROOT
from site_copilot.demos import listar

APP = str(ROOT / "src" / "site_copilot" / "app.py")


@pytest.fixture
def sin_keys(monkeypatch):
    # Variables vacías: load_dotenv no sobrescribe las ya definidas, así que el .env local no cuenta.
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(var, "")


def _textos(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def test_sin_key_no_hay_chat_pero_si_opcion_de_key_propia(sin_keys):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert len(at.chat_input) == 0
    assert len(at.text_input) == 1  # campo opcional para la key del visitante


def test_modo_publico_solo_ejemplos(monkeypatch):
    # Aunque haya key en el entorno, el modo público no expone chat ni campo de key.
    monkeypatch.setenv("SITE_COPILOT_PUBLICO", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-no-se-usa")
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert len(at.chat_input) == 0 and len(at.text_input) == 0


def test_cada_ejemplo_se_puede_abrir(sin_keys):
    demos = listar()
    assert len(demos) >= 10, "Se esperan al menos 10 ejemplos grabados en demos/"
    at = AppTest.from_file(APP, default_timeout=60).run()
    for i, d in enumerate(demos):
        at = at.button(key=f"demo{i}").click().run()
        assert not at.exception
        assert d["resultado"].texto.strip()[:80] in _textos(at)
