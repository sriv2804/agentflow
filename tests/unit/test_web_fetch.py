import asyncio

import httpx
import pytest

from src.tools import web_fetch as web_fetch_module
from src.tools.web_fetch import extract_text, web_fetch

PAGE = """<html><head><style>.x{}</style><script>track()</script></head><body>
<header>Site header</header><nav>Menu</nav>
<main><h1>Pricing</h1><p>Pro plan: $10 per user / month</p></main>
<footer>Copyright</footer></body></html>"""


def test_extract_text_strips_boilerplate_tags():
    text = extract_text(PAGE)
    assert text == "Pricing\nPro plan: $10 per user / month"


def test_extract_text_truncates_to_word_limit():
    text = extract_text("<p>" + "word\n" * 50 + "</p>", max_words=10)
    assert text.split("\n\n")[0].split() == ["word"] * 10
    assert "[truncated to the first 10 words]" in text


@pytest.fixture
def mock_http(monkeypatch):
    def install(handler):
        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            web_fetch_module.httpx, "AsyncClient",
            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
        )
    return install


def test_fetch_returns_clean_text(mock_http):
    mock_http(lambda request: httpx.Response(200, text=PAGE))
    assert asyncio.run(web_fetch("https://example.com/pricing")) == "Pricing\nPro plan: $10 per user / month"


def test_fetch_reports_http_errors(mock_http):
    mock_http(lambda request: httpx.Response(404))
    assert asyncio.run(web_fetch("https://example.com/missing")) == "Fetch failed: HTTP 404 for https://example.com/missing"


def test_fetch_reports_timeouts(mock_http):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)
    mock_http(timeout)
    assert asyncio.run(web_fetch("https://example.com/slow")).startswith("Fetch failed: timed out")
