import re
import socket
import time
from ipaddress import ip_address
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from app.config import get_settings

MAX_SEARCH_RESULTS = 5
MAX_DIRECT_URLS = 4
MAX_SOURCE_CHARS = 8000
REQUEST_TIMEOUT_SECONDS = 12
URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+")


@dataclass
class WebSource:
    title: str
    url: str
    content: str


@dataclass
class WebContextResult:
    context: str | None
    status: str
    provider: str       # "Tavily" | "Brave" | "DuckDuckGo" | "" (URL-only crawl)
    sources_count: int
    search_query: str | None


class ReadableTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.meta_description: str | None = None
        self.tables: list[list[list[str]]] = []
        self.skip_depth = 0
        self.current_tag: str | None = None
        self.in_table = False
        self.in_cell = False
        self.current_cell: list[str] = []
        self.current_row: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = {str(k).lower(): str(v) for k, v in attrs if v is not None}
        if tag == "meta":
            name = attrs_dict.get("name", attrs_dict.get("property", "")).lower()
            if name in {"description", "og:description"} and attrs_dict.get("content"):
                self.meta_description = attrs_dict["content"].strip()
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        self.current_tag = tag
        if tag == "table":
            self.in_table = True
            self.tables.append([])
        if tag == "tr" and self.in_table:
            self.current_row = []
        if tag in {"td", "th"} and self.in_table:
            self.in_cell = True
            self.current_cell = []
        if not self.in_table and tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth > 0:
            self.skip_depth -= 1
        if tag in {"td", "th"} and self.in_table:
            cell = normalize_text(" ".join(self.current_cell))
            self.current_row.append(cell)
            self.current_cell = []
            self.in_cell = False
        if tag == "tr" and self.in_table:
            row = [c for c in self.current_row if c]
            if row and self.tables:
                self.tables[-1].append(row)
            self.current_row = []
        if tag == "table":
            self.in_table = False
        if not self.in_table and tag in {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}:
            self.parts.append("\n")
        if self.current_tag == tag:
            self.current_tag = None

    def handle_data(self, data: str) -> None:
        if self.skip_depth > 0:
            return
        text = data.strip()
        if not text:
            return
        if self.current_tag == "title":
            self.title_parts.append(text)
        if self.in_cell:
            self.current_cell.append(text)
            return
        if not self.in_table:
            self.parts.append(text)

    def title(self) -> str | None:
        title = normalize_text(" ".join(self.title_parts))
        return title or None

    def table_markdown(self) -> str:
        sections: list[str] = []
        for rows in self.tables[:6]:
            if not rows:
                continue
            width = max(len(r) for r in rows)
            if width < 2:
                continue
            normalized = [r + [""] * (width - len(r)) for r in rows[:12]]
            header = "| " + " | ".join(normalized[0]) + " |"
            sep = "| " + " | ".join(["---"] * width) + " |"
            body = "\n".join("| " + " | ".join(r) + " |" for r in normalized[1:])
            sections.append("\n".join([header, sep, body]) if body else "\n".join([header, sep]))
        return "\n\n".join(sections)

    def text(self) -> str:
        chunks: list[str] = []
        if self.meta_description:
            chunks.append(f"Description: {self.meta_description}")
        readable = normalize_text(" ".join(self.parts))
        if readable:
            chunks.append(readable)
        tables = self.table_markdown()
        if tables:
            chunks.append(f"Tables:\n{tables}")
        return normalize_text("\n\n".join(chunks))


def normalize_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return value.strip()


def find_urls(message: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in URL_RE.findall(message):
        url = match.rstrip(".,;:")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls[:MAX_DIRECT_URLS]


def source_title(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or url


def is_public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.hostname in {"localhost", "0.0.0.0"}:
        return False
    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or default_port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for address in addresses:
        host = address[4][0]
        try:
            ip = ip_address(host)
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def extract_html_document(html: str) -> tuple[str | None, str]:
    parser = ReadableTextParser()
    parser.feed(html)
    return parser.title(), parser.text()


def extract_text_from_html(html: str) -> str:
    return extract_html_document(html)[1]


def extract_text_from_pdf(content: bytes, max_pages: int = 12) -> str:
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=content, filetype="pdf")
        parts: list[str] = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                parts.append(f"[PDF truncated after {max_pages} pages out of {doc.page_count}.]")
                break
            text = page.get_text("text").strip()
            if text:
                parts.append(f"--- Page {i + 1} ---\n{text}")
        return normalize_text("\n\n".join(parts))
    except Exception:
        return ""


def crawl_url(url: str) -> WebSource | None:
    if not is_public_http_url(url):
        return None
    try:
        headers = {"User-Agent": "FroneiBot/0.1 (+https://fronei.com)"}
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers=headers) as client:
            response = client.get(url)
            response.raise_for_status()
        if not is_public_http_url(str(response.url)):
            return None
        content_type = response.headers.get("content-type", "")
        final_url = str(response.url)
        title = source_title(final_url)
        if "application/pdf" in content_type or urlparse(final_url).path.lower().endswith(".pdf"):
            text = extract_text_from_pdf(response.content)
            if text:
                title = f"{title} PDF"
        elif "text/html" in content_type or "<html" in response.text[:300].lower():
            html_title, text = extract_html_document(response.text)
            title = html_title or title
        else:
            text = normalize_text(response.text)
        if not text:
            return None
        return WebSource(title=title, url=final_url, content=text[:MAX_SOURCE_CHARS])
    except Exception:
        return None


def tavily_search(query: str) -> list[WebSource]:
    settings = get_settings()
    if not settings.tavily_api_key:
        return []
    payload = {
        "query": query,
        "search_depth": "advanced",
        "max_results": MAX_SEARCH_RESULTS,
        "include_answer": False,
        "include_raw_content": "text",
    }
    headers = {
        "Authorization": f"Bearer {settings.tavily_api_key}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = client.post("https://api.tavily.com/search", json=payload, headers=headers)
            response.raise_for_status()
        data = response.json()
    except Exception:
        return []
    sources: list[WebSource] = []
    for item in data.get("results", [])[:MAX_SEARCH_RESULTS]:
        url = item.get("url") or ""
        content = item.get("raw_content") or item.get("content") or ""
        title = item.get("title") or source_title(url)
        if url and content:
            sources.append(WebSource(title=title, url=url, content=normalize_text(content)[:MAX_SOURCE_CHARS]))
    return sources


def brave_search(query: str) -> list[WebSource]:
    settings = get_settings()
    if not settings.brave_api_key:
        return []
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": settings.brave_api_key,
    }
    params = {"q": query, "count": MAX_SEARCH_RESULTS, "text_decorations": False}
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
        data = response.json()
    except Exception:
        return []
    sources: list[WebSource] = []
    for item in data.get("web", {}).get("results", [])[:MAX_SEARCH_RESULTS]:
        url = item.get("url", "")
        title = item.get("title", source_title(url))
        description = item.get("description", "")
        snippets = " ".join(item.get("extra_snippets", []))
        content = f"{description} {snippets}".strip()
        if url and content:
            sources.append(WebSource(title=title, url=url, content=normalize_text(content)[:MAX_SOURCE_CHARS]))
    return sources


def test_tavily_connection() -> dict:
    """Minimal live ping to verify the Tavily key works. Used by the admin Providers tab."""
    settings = get_settings()
    if not settings.tavily_api_key:
        return {"success": False, "error": "TAVILY_API_KEY not configured."}
    payload = {"query": "ping", "search_depth": "basic", "max_results": 1, "include_answer": False}
    headers = {"Authorization": f"Bearer {settings.tavily_api_key}", "Content-Type": "application/json"}
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = client.post("https://api.tavily.com/search", json=payload, headers=headers)
            response.raise_for_status()
        return {"success": True, "latency_ms": int((time.perf_counter() - started) * 1000)}
    except httpx.HTTPStatusError as exc:
        return {"success": False, "latency_ms": int((time.perf_counter() - started) * 1000), "error": f"HTTP {exc.response.status_code}"}
    except Exception as exc:
        return {"success": False, "latency_ms": int((time.perf_counter() - started) * 1000), "error": str(exc)[:300]}


def test_brave_connection() -> dict:
    """Minimal live ping to verify the Brave key works. Used by the admin Providers tab."""
    settings = get_settings()
    if not settings.brave_api_key:
        return {"success": False, "error": "BRAVE_API_KEY not configured."}
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": settings.brave_api_key,
    }
    params = {"q": "ping", "count": 1}
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
        return {"success": True, "latency_ms": int((time.perf_counter() - started) * 1000)}
    except httpx.HTTPStatusError as exc:
        return {"success": False, "latency_ms": int((time.perf_counter() - started) * 1000), "error": f"HTTP {exc.response.status_code}"}
    except Exception as exc:
        return {"success": False, "latency_ms": int((time.perf_counter() - started) * 1000), "error": str(exc)[:300]}


def ddg_search(query: str) -> list[WebSource]:
    try:
        from ddgs import DDGS
    except ImportError:
        return []
    try:
        sources: list[WebSource] = []
        with DDGS() as ddgs:
            for result in ddgs.text(query, max_results=MAX_SEARCH_RESULTS):
                url = result.get("href", "")
                title = result.get("title", source_title(url))
                body = result.get("body", "")
                if url and body:
                    sources.append(WebSource(title=title, url=url, content=normalize_text(body)[:MAX_SOURCE_CHARS]))
        return sources
    except Exception:
        return []


def gather_web_context(query: str, enable_search: bool) -> WebContextResult:
    direct_sources = [source for url in find_urls(query) if (source := crawl_url(url))]

    search_sources: list[WebSource] = []
    search_provider = ""
    if enable_search:
        settings = get_settings()
        if settings.tavily_api_key:
            search_sources = tavily_search(query)
            search_provider = "Tavily"
        elif settings.brave_api_key:
            search_sources = brave_search(query)
            search_provider = "Brave"
        else:
            search_sources = ddg_search(query)
            search_provider = "DuckDuckGo" if search_sources else ""

    sources: list[WebSource] = []
    seen_urls: set[str] = set()
    for source in [*direct_sources, *search_sources]:
        if source.url not in seen_urls:
            seen_urls.add(source.url)
            sources.append(source)

    if not sources:
        if enable_search and not search_provider:
            status = "Web search requested — no key configured (TAVILY_API_KEY / BRAVE_API_KEY) and DuckDuckGo returned no results."
        elif enable_search:
            status = f"Web search via {search_provider} returned no results."
        else:
            status = "Web context not requested."
        return WebContextResult(context=None, status=status, provider=search_provider,
                                sources_count=0, search_query=query if enable_search else None)

    sections = []
    for idx, source in enumerate(sources, start=1):
        sections.append(f"[S{idx}] {source.title}\nURL: {source.url}\nExcerpt:\n{source.content}")

    parts: list[str] = []
    if direct_sources:
        parts.append(f"{len(direct_sources)} crawled URL(s)")
    if search_sources:
        parts.append(f"{len(search_sources)} search result(s) via {search_provider}")
    status = "Retrieved " + " + ".join(parts) + "."

    return WebContextResult(
        context="\n\n".join(sections),
        status=status,
        provider=search_provider if search_sources else "",
        sources_count=len(sources),
        search_query=query if enable_search else None,
    )
