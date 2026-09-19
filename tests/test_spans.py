"""Tests for span normalisation and lookup. Pure functions, no model."""

from simpleaudit.spans import MIN_SPAN_CHARS, assistant_texts, find_span, normalise


def test_normalise_casefolds_and_collapses_whitespace():
    assert normalise("  Seks   UKER\n\tfra   vedtak ") == "seks uker fra vedtak"


def test_normalise_strips_markdown_emphasis_headers_and_bullets():
    text = "# Hovedregel\n\n- **Inntil 2 år ekstra** hvis du _nesten_ er i jobb\n`kode`"
    assert normalise(text) == "hovedregel inntil 2 år ekstra hvis du nesten er i jobb kode"


def test_normalise_folds_unicode_quotes_dashes_and_nbsp():
    assert normalise("«50 000» – “kr”") == "50 000 kr"
    assert normalise("It’s ‘fine’") == "it s fine"


def test_normalise_keeps_norwegian_letters_and_digits():
    assert normalise("Skatteoppgjøret 2026: Blå resept, æøå ÆØÅ") == "skatteoppgjøret 2026 blå resept æøå æøå"


def test_normalise_drops_emoji():
    assert normalise("Hei! 👋 Her er svaret ✅") == "hei her er svaret"


def test_normalise_handles_none_and_empty():
    assert normalise(None) == ""
    assert normalise("") == ""
    assert normalise("***") == ""


def test_find_span_returns_first_matching_text_index():
    texts = ["Fristen er 30. april.", "Klagefristen er seks uker fra du mottok vedtaket.", "seks uker igjen"]
    assert find_span("seks uker fra du mottok", texts) == 1


def test_find_span_none_when_absent():
    assert find_span("dette står ingen steder", ["noe helt annet her"]) is None


def test_find_span_never_matches_across_texts():
    texts = ["Første tur slutter med disse ordene", "og andre tur begynner med disse"]
    assert find_span("disse ordene og andre tur", texts) is None


def test_find_span_rejects_short_spans():
    texts = ["Ja, det stemmer. Ring 22 07 00 00 for hjelp."]
    assert find_span("ja", texts) is None
    assert len(normalise("22 07 00 00")) >= MIN_SPAN_CHARS
    assert find_span("22 07 00 00", texts) == 0
    assert find_span("ja det", texts, min_chars=3) == 0


def test_find_span_matches_quote_with_markdown_removed_and_case_changed():
    texts = ["Den normale maksperioden er **3 år**.\n\n## Unntak\n- **Inntil 2 år ekstra**"]
    assert find_span("inntil 2 år ekstra", texts) == 0
    assert find_span("Den normale maksperioden er 3 år", texts) == 0


def test_assistant_texts_filters_roles_and_keeps_alignment():
    conversation = [
        {"role": "user", "content": "spørsmål"},
        {"role": "assistant", "content": "svar én"},
        {"role": "user", "content": "oppfølging"},
        {"role": "assistant", "content": [{"type": "text", "text": "blokk"}]},
        {"role": "assistant", "content": "svar tre"},
    ]
    assert assistant_texts(conversation) == ["svar én", "", "svar tre"]
