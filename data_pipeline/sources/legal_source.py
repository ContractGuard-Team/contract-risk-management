"""vanban.chinhphu.vn legal document list (ASP.NET WebForms): parse rows, page through postbacks, read details.

Paging has no URL parameter: each next page is a form POST carrying the previous page's __VIEWSTATE with
__EVENTARGUMENT=Page$N. The list order is not stable between postbacks, so rows are deduplicated by docid and
the deduplicated count is reconciled with the site total; a per-year search sweeps up what a pass missed.
"""
import asyncio
from datetime import date
import re
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

GRID = re.compile(r"grvDocument$")
TOTAL = re.compile(r"(\d+)\s*-\s*(\d+)\s*\|\s*(\d+)")
ATTACHMENT = re.compile(r"datafiles\.chinhphu\.vn/.+\.[A-Za-z0-9]{2,5}($|\?)", re.I)


def _date(text):
    match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text or "")
    return date(int(match[3]), int(match[2]), int(match[1])).isoformat() if match else None


def _docid(href):
    values = parse_qs(urlsplit(href).query).get("docid")
    return values[0] if values else None


def form_fields(soup):
    """Every value the browser would post back: hidden/text inputs and selected options (no submit buttons)."""
    fields = {}
    for field in soup.find_all("input"):
        if field.get("name") and field.get("type", "text") in ("hidden", "text"):
            fields[field["name"]] = field.get("value", "")
    for select in soup.find_all("select"):
        if select.get("name"):
            chosen = select.find("option", selected=True) or select.find("option")
            fields[select["name"]] = chosen.get("value", "") if chosen else ""
    return fields


def parse_list(html, base_url):
    soup = BeautifulSoup(html, "lxml")
    grid = soup.find("table", id=GRID)
    if grid is None:
        raise ValueError("Document grid not found: the source page layout changed")
    rows, pager = [], None
    for tr in grid.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if any("Page$" in (a.get("href") or "") for a in tr.find_all("a")):
            pager = tr
            continue
        if len(cells) < 3:
            continue
        link = cells[0].find("a", href=True)
        docid = _docid(link["href"]) if link else None
        if not docid:
            continue
        code = cells[0].find(class_="code")
        rows.append({
            "docid": docid,
            "symbol": (code.get_text(" ", strip=True) if code else link.get_text(" ", strip=True)).strip(),
            "issued_on": _date(cells[1].get_text(" ", strip=True)),
            "title": " ".join((cells[2].find(class_="substract") or cells[2]).get_text(" ", strip=True).split()),
            "detail_url": urljoin(base_url, link["href"]),
            "files": [urljoin(base_url, a["href"]) for a in cells[2].select(".bl-doc-files a[href]")],
        })
    total_match = TOTAL.search(soup.get_text(" "))
    current = next_page = event_target = None
    if pager is not None:
        event_target = re.search(r"__doPostBack\('([^']+)'", pager.find("a", href=True)["href"])[1]
        span = pager.find("span")
        current = int(span.get_text(strip=True)) if span and span.get_text(strip=True).isdigit() else 1
        targets = {re.search(r"'(Page\$[^']+)'", a["href"])[1] for a in pager.find_all("a", href=True) if "Page$" in a["href"]}
        next_page = f"Page${current + 1}" if f"Page${current + 1}" in targets else None
    return {"rows": rows, "total": int(total_match[3]) if total_match else None, "page": current or 1,
            "next": next_page, "event_target": event_target, "form": form_fields(soup)}


def parse_detail(html, base_url):
    soup = BeautifulSoup(html, "lxml")
    fields = {}
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) >= 2:
            fields.setdefault(cells[0].get_text(" ", strip=True).rstrip(":"), cells[1])
    def text(label):
        cell = fields.get(label)
        return " ".join(cell.get_text(" ", strip=True).split()) if cell is not None else ""
    files = []  # every attached document (.pdf, .doc, .docx, .rtf, …), in page order
    for anchor in soup.find_all("a", href=ATTACHMENT):
        url = urljoin(base_url, anchor["href"])
        if url not in files:
            files.append(url)
    return {"symbol": text("Số ký hiệu"), "issued_on": _date(text("Ngày ban hành")),
            "effective_on": _date(text("Ngày có hiệu lực")), "doc_type": text("Loại văn bản"),
            "issuer": text("Cơ quan ban hành"), "signer": text("Người ký"), "title": text("Trích yếu"),
            "files": files}


class LegalSource:
    """Polite crawler: one request at a time for paging (VIEWSTATE chains), bounded concurrency for downloads."""

    def __init__(self, list_urls, *, client=None, delay=1.0, concurrency=3, user_agent="contract-risk-data-collector/1.0",
                 timeout=60, max_file_bytes=50 * 1024 * 1024):
        # Hiến pháp (typegroupid=1) and Luật/Bộ luật (typegroupid=3) are separate listings on the site.
        self.list_urls = [list_urls] if isinstance(list_urls, str) else list(list_urls)
        self.delay = delay
        self.client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                                  headers={"User-Agent": user_agent})
        self.semaphore = asyncio.Semaphore(concurrency)
        self.max_file_bytes = max_file_bytes

    async def _request(self, method, url, **kwargs):
        for attempt in range(3):
            try:
                response = await self.client.request(method, url, **kwargs)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise httpx.TransportError(f"HTTP {response.status_code}")
                response.raise_for_status()
                return response
            except httpx.TransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep(self.delay * 2 ** (attempt + 1))
            finally:
                await asyncio.sleep(self.delay)

    async def _walk(self, url, page, stop_when=None):
        """Yield every page reachable by Page$N+1 postbacks starting from an already parsed page."""
        while True:
            yield page
            if not page["next"] or (stop_when and stop_when(page)):
                return
            data = {**page["form"], "__EVENTTARGET": page["event_target"], "__EVENTARGUMENT": page["next"]}
            response = await self._request("POST", url, data=data)
            page = parse_list(response.text, url)

    async def list_documents(self, *, stop_when=None, sweep_years=True):
        """Return ({docid: row}, report) over every listing URL. stop_when(page) lets incremental runs stop early."""
        seen, report = {}, {"pages": 0, "total_on_site": 0, "missing": 0}
        for url in self.list_urls:
            rows, part = await self._list_one(url, stop_when, sweep_years)
            for docid, row in rows.items():
                seen.setdefault(docid, row)
            report["pages"] += part["pages"]
            report["total_on_site"] += part["total_on_site"] or 0
            report["missing"] = None if None in (report["missing"], part["missing"]) else report["missing"] + part["missing"]
        return seen, {**report, "seen": len(seen)}

    async def _list_one(self, url, stop_when, sweep_years):
        first = parse_list((await self._request("GET", url)).text, url)
        seen, pages = {}, 0
        async for page in self._walk(url, first, stop_when):
            pages += 1
            for row in page["rows"]:
                seen.setdefault(row["docid"], row)
        total = first["total"]
        stopped_early = stop_when is not None and len(seen) < (total or 0)
        if sweep_years and not stopped_early and total and len(seen) < total:
            year_field = next((k for k in first["form"] if k.endswith("drdDocYear")), None)
            button = next((k for k in first["form"] if k.endswith("txtSearchKeyword")), "").replace("txtSearchKeyword", "btnSearch")
            soup = BeautifulSoup((await self._request("GET", url)).text, "lxml")
            years = [o.get("value") for o in soup.find("select", attrs={"name": year_field}).find_all("option")
                     if (o.get("value") or "0") != "0"] if year_field else []
            for year in years:
                if len(seen) >= total:
                    break
                data = {**first["form"], year_field: year, button: "Tìm kiếm", "__EVENTTARGET": "", "__EVENTARGUMENT": ""}
                filtered = parse_list((await self._request("POST", url, data=data)).text, url)
                async for page in self._walk(url, filtered):
                    pages += 1
                    for row in page["rows"]:
                        seen.setdefault(row["docid"], row)
        return seen, {"pages": pages, "seen": len(seen), "total_on_site": total,
                      "missing": max(0, (total or 0) - len(seen)) if not stopped_early else None}

    async def detail(self, row):
        async with self.semaphore:
            response = await self._request("GET", row["detail_url"])
        return parse_detail(response.text, row["detail_url"])

    async def download(self, url):
        async with self.semaphore:
            response = await self._request("GET", url)
        if len(response.content) > self.max_file_bytes:
            raise ValueError("Attachment exceeds the size limit")
        head = response.content.lstrip()[:15].lower()
        if not head or head.startswith((b"<!doctype", b"<html")):  # error page instead of the file
            raise ValueError("Attachment URL returned no document")
        return response.content

    async def aclose(self):
        await self.client.aclose()
