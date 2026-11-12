"""公告采集：抓取同站分页列表与全部详情页，失败逐条记录。

详情页处理顺序（人工纠偏优先于自动解析）：
1. 抓取详情原文并计算内容 SHA-256；
2. 若纠偏库中存在 (URL, 当前哈希) 的记录，直接套用人工核正字段，
   即使原文无法自动解析也能正常参与核对；
3. 若该 URL 存在纠偏记录但哈希已不匹配（原文变化），旧纠偏失效：
   该地址进入待复核（pending_review），既不套用旧字段，也不采信
   未经复核的自动解析结果；
4. 否则按常规 BeautifulSoup 解析；解析/抓取失败逐条登记。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .corrections import CorrectionRecord, CorrectionStore, get_store
from .parser import (
    ClosureAnnouncement,
    Listing,
    content_hash,
    extract_title,
    normalize_url,
    parse_detail,
    parse_listing,
)
from .timeutils import ClockRange

MAX_PAGES = 50  # 防止分页环路导致的无限抓取
REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
RETRIES = 1  # 失败后再尝试 1 次（共 2 次请求）


@dataclass
class FetchFailureRecord:
    url: str
    stage: str
    reason: str


@dataclass
class PendingReviewRecord:
    """内容已变化、旧纠偏失效、等待人工重新核正的详情地址。"""

    url: str
    reason: str


@dataclass
class ScrapeResult:
    announcements: list[ClosureAnnouncement] = field(default_factory=list)
    failures: list[FetchFailureRecord] = field(default_factory=list)
    pending_review: list[PendingReviewRecord] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and not self.pending_review


async def _get(client: httpx.AsyncClient, url: str) -> str:
    last_exc: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPError as exc:  # 含状态码错误、超时、连接错误
            last_exc = exc
    raise last_exc  # type: ignore[misc]


async def fetch_detail_html(url: str) -> str:
    """供纠偏接口使用：单独抓取一个详情页原文（失败由调用方转 4xx/5xx）。"""
    headers = {"User-Agent": "coldchain-dispatch-checker/1.0"}
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers
    ) as client:
        return await _get(client, url)


def correction_to_announcement(
    record: CorrectionRecord, html: str
) -> ClosureAnnouncement:
    """把人工核正记录转成核对流程使用的结构化公告。"""
    return ClosureAnnouncement(
        url=record.detail_url,
        title=record.title or extract_title(html),
        road_id=record.road_id,
        direction=record.direction,
        effective_start=record.effective_start,
        effective_end=record.effective_end,
        weekdays=list(record.weekdays),
        clock=ClockRange(
            start=record.clock_start,
            end=record.clock_end,
            cross_midnight=record.cross_midnight,
        ),
        raw_effective=record.effective_start.isoformat(),
        raw_weekdays=",".join(str(d) for d in record.weekdays),
        raw_clock=(
            f"{record.clock_start.strftime('%H:%M')}-"
            f"{record.clock_end.strftime('%H:%M')}"
        ),
    )


async def scrape(
    list_url: str, store: CorrectionStore | None = None
) -> ScrapeResult:
    """采集入口：分页列表 -> 去重详情链接 -> 逐页解析/套用纠偏。"""
    store = store or get_store()
    result = ScrapeResult()
    headers = {"User-Agent": "coldchain-dispatch-checker/1.0"}

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers
    ) as client:
        detail_urls = await _collect_listing_pages(client, list_url, result)

        for detail_url in detail_urls:
            try:
                html = await _get(client, detail_url)
            except Exception as exc:  # 详情抓取失败：记录并继续
                result.failures.append(
                    FetchFailureRecord(
                        url=detail_url, stage="detail", reason=describe_error(exc)
                    )
                )
                continue

            digest = content_hash(html)
            correction = store.get(detail_url, digest)
            if correction is not None:
                # 同一内容：始终使用人工核正字段，哪怕原文无法自动解析
                result.announcements.append(
                    correction_to_announcement(correction, html)
                )
                continue

            if store.any_for_url(detail_url):
                # 原文已变化：旧纠偏失效，转待复核，不采信自动解析
                result.pending_review.append(
                    PendingReviewRecord(
                        url=detail_url,
                        reason="页面内容已变化，原有纠偏失效，需人工重新核正",
                    )
                )
                continue

            try:
                result.announcements.append(parse_detail(html, detail_url))
            except Exception as exc:  # 解析失败：记录并继续，绝不静默丢弃
                result.failures.append(
                    FetchFailureRecord(
                        url=detail_url, stage="detail", reason=describe_error(exc)
                    )
                )

    return result


async def _collect_listing_pages(
    client: httpx.AsyncClient, first_url: str, result: ScrapeResult
) -> list[str]:
    """遍历同站分页，汇总去重后的详情链接。"""
    collected: list[str] = []
    seen_pages: set[str] = set()
    seen_details: set[str] = set()
    url = normalize_url(first_url)

    for _ in range(MAX_PAGES):
        if url in seen_pages:
            break
        seen_pages.add(url)

        try:
            html = await _get(client, url)
        except Exception as exc:
            result.failures.append(
                FetchFailureRecord(url=url, stage="list", reason=describe_error(exc))
            )
            break

        listing: Listing = parse_listing(html, url)
        for link in listing.detail_links:
            key = normalize_url(link)
            if key not in seen_details:
                seen_details.add(key)
                collected.append(link)

        if not listing.next_page_url:
            break
        url = normalize_url(listing.next_page_url)

    return collected


def describe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时"
    if isinstance(exc, httpx.ConnectError):
        return "连接失败"
    return f"{type(exc).__name__}: {exc}"
