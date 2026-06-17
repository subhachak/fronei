from types import SimpleNamespace

import app.services.web_context as web_context
from app.services.web_context import (
    WebSource,
    extract_html_document,
    extract_text_from_pdf,
    extract_text_from_html,
    normalize_text,
    search_web_sources,
)


def test_extract_html_document_keeps_title_description_and_table():
    html = """
    <html>
      <head>
        <title>Vendor pricing</title>
        <meta name="description" content="Current pricing table for the platform">
      </head>
      <body>
        <script>ignore me</script>
        <h1>Pricing</h1>
        <p>Enterprise tier includes governance.</p>
        <table>
          <tr><th>Tier</th><th>Price</th></tr>
          <tr><td>Pro</td><td>$10</td></tr>
        </table>
      </body>
    </html>
    """
    title, text = extract_html_document(html)
    assert title == "Vendor pricing"
    assert "Description: Current pricing table" in text
    assert "Enterprise tier includes governance." in text
    assert "| Tier | Price |" in text
    assert "| Pro | $10 |" in text
    assert "ignore me" not in text


def test_extract_text_from_html_backcompat_returns_text_only():
    assert "Hello world" in extract_text_from_html("<html><body><p>Hello world</p></body></html>")


def test_normalize_text_fixes_spacing_before_punctuation():
    assert normalize_text("Hello   world  !") == "Hello world!"


def test_extract_text_from_pdf_reads_pages():
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "PDF research content")
    content = doc.tobytes()
    doc.close()

    text = extract_text_from_pdf(content)
    assert "PDF research content" in text
    assert "Page 1" in text


def test_search_web_sources_prefers_you_before_tavily(monkeypatch):
    monkeypatch.setattr(
        web_context,
        "get_settings",
        lambda: SimpleNamespace(you_api_key="you-key", tavily_api_key="tavily-key", nimble_api_key=None),
    )
    calls: list[str] = []

    def fake_you(query, recency=None):
        calls.append("You.com")
        return [WebSource("You result", "https://you.example", "content")]

    def fake_tavily(query, recency=None):
        calls.append("Tavily")
        return [WebSource("Tavily result", "https://tavily.example", "content")]

    monkeypatch.setattr(web_context, "you_search", fake_you)
    monkeypatch.setattr(web_context, "tavily_search", fake_tavily)

    provider, sources = search_web_sources("agent search")

    assert provider == "You.com"
    assert sources[0].url == "https://you.example"
    assert calls == ["You.com"]


def test_you_search_uses_documented_endpoint_and_response_shape(monkeypatch):
    monkeypatch.setattr(
        web_context,
        "get_settings",
        lambda: SimpleNamespace(you_api_key="you-key", tavily_api_key=None, nimble_api_key=None),
    )
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": {
                    "web": [
                        {
                            "title": "You result",
                            "url": "https://you.example/result",
                            "snippets": ["You.com snippet"],
                        }
                    ]
                }
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            return FakeResponse()

    monkeypatch.setattr(web_context.httpx, "Client", FakeClient)

    sources = web_context.you_search("agent search", recency="week")

    assert captured["url"] == "https://ydc-index.io/v1/search"
    assert captured["headers"] == {"X-API-Key": "you-key"}
    assert captured["params"] == {"query": "agent search", "count": web_context.MAX_SEARCH_RESULTS, "freshness": "week"}
    assert sources[0].title == "You result"
    assert sources[0].url == "https://you.example/result"
    assert sources[0].content == "You.com snippet"


def test_search_web_sources_falls_back_to_tavily_when_you_empty(monkeypatch):
    monkeypatch.setattr(
        web_context,
        "get_settings",
        lambda: SimpleNamespace(you_api_key="you-key", tavily_api_key="tavily-key", nimble_api_key=None),
    )
    calls: list[str] = []
    monkeypatch.setattr(web_context, "you_search", lambda query, recency=None: calls.append("You.com") or [])
    monkeypatch.setattr(
        web_context,
        "tavily_search",
        lambda query, recency=None: calls.append("Tavily") or [WebSource("T", "https://t.example", "content")],
    )

    provider, sources = search_web_sources("agent search")

    assert provider == "Tavily"
    assert sources[0].url == "https://t.example"
    assert calls == ["You.com", "Tavily"]


def test_search_web_sources_falls_back_to_nimble_when_you_and_tavily_empty(monkeypatch):
    monkeypatch.setattr(
        web_context,
        "get_settings",
        lambda: SimpleNamespace(
            you_api_key="you-key",
            tavily_api_key="tavily-key",
            nimble_api_key="nimble-key",
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(web_context, "you_search", lambda query, recency=None: calls.append("You.com") or [])
    monkeypatch.setattr(web_context, "tavily_search", lambda query, recency=None: calls.append("Tavily") or [])
    monkeypatch.setattr(
        web_context,
        "nimble_search",
        lambda query, recency=None: calls.append("Nimble") or [WebSource("N", "https://n.example", "content")],
    )

    provider, sources = search_web_sources("agent search")

    assert provider == "Nimble"
    assert sources[0].url == "https://n.example"
    assert calls == ["You.com", "Tavily", "Nimble"]
