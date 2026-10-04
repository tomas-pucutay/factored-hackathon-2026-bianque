import pytest

from bianque.agent.llm import GeminiExtractor, LLMUnavailable, MenuParser, redact


def test_redact_removes_ids_cards_phones_and_emails_but_keeps_amounts():
    text = "soy 12345678, tarjeta 4111 1111 1111 1111, mail a.b@x.com, cobro de 450 dolares"

    out = redact(text)

    assert "12345678" not in out and "4111" not in out and "a.b@x.com" not in out
    assert out.count("[number]") == 2 and "[email]" in out
    assert "450" in out  # short numbers (amounts) stay


def test_menu_parser_understands_the_fallback_menu_in_both_languages():
    menu = MenuParser()

    assert menu.extract("2", "", "").intent == "not_mine"
    assert menu.extract("1", "", "").intent == "its_mine"
    assert menu.extract("sí", "", "").confirmation == "yes"
    assert menu.extract("não", "", "").confirmation == "no"
    assert menu.extract("não", "", "").language == "pt"
    assert menu.extract("hola, qué tal", "", "").intent == "unclear"


def test_gemini_without_a_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(LLMUnavailable):
        GeminiExtractor()
