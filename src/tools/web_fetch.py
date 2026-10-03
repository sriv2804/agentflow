import re
import httpx
from bs4 import BeautifulSoup
from src.tools.common import tool

MAX_WORDS = 3000
TIMEOUT_SECONDS = 15
STRIP_TAGS = ["script", "style", "nav", "footer", "header"]
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; agentflow-web-fetch/0.1)"}


def extract_text(html: str, max_words: int = MAX_WORDS) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(STRIP_TAGS):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    word_ends = [m.end() for m in re.finditer(r"\S+", text)]
    if len(word_ends) <= max_words:
        return text
    return text[:word_ends[max_words - 1]] + f"\n\n[truncated to the first {max_words} words]"


@tool(name="web_fetch", description="Fetch a web page and return its main text content")
async def web_fetch(url: str, _ctx=None) -> str:
    """
    Fetch the full text of a web page, e.g. a pricing page found via web_search.

    Args:
        url: The full http(s) URL of the page to fetch
    """
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, follow_redirects=True, headers=HEADERS
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.TimeoutException:
        return f"Fetch failed: timed out after {TIMEOUT_SECONDS}s for {url}"
    except httpx.HTTPStatusError as e:
        return f"Fetch failed: HTTP {e.response.status_code} for {url}"
    except httpx.HTTPError as e:
        return f"Fetch failed: {type(e).__name__}: {e} for {url}"

    try:
        text = extract_text(response.text)
    except Exception as e:
        return f"Fetch failed: could not parse {url}: {e}"
    return text or f"Fetch returned no readable text for {url}"
