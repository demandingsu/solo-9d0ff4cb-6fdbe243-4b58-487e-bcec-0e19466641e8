"""FastAPI 入口：冷链配送施工封闭核对 API。"""
from __future__ import annotations

import httpx
from fastapi import FastAPI, HTTPException

from .corrections import CorrectionRecord, get_store
from .models import (
    CheckRequest,
    CheckResponse,
    CorrectionRequest,
    CorrectionResponse,
)
from .parser import content_hash, extract_title
from .scheduler import build_response
from .scraper import describe_error, fetch_detail_html, scrape

app = FastAPI(
    title="冷链配送施工封闭核对 API",
    description=(
        "传入公告列表页 URL 与计划路段（编号、方向、带时区驶入/驶出时间），"
        "采集同站分页与详情并返回受阻路段及公告链接；"
        "采集失败或纠偏失效待复核时标记无法判定并给出对应地址。"
        "另提供 /corrections 人工纠偏接口，以详情内容 SHA-256 为条件保存核正字段。"
    ),
    version="1.1.0",
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/check", response_model=CheckResponse)
async def check(request: CheckRequest) -> CheckResponse:
    scrape_result = await scrape(request.announcement_list_url)
    return build_response(request.segments, scrape_result)


@app.post("/corrections", response_model=CorrectionResponse, status_code=201)
async def create_correction(request: CorrectionRequest) -> CorrectionResponse:
    """人工纠偏：实时读取详情页，按内容 SHA-256 条件保存核正字段。

    - 成功抓取后计算页面正文 SHA-256，纠偏记录与 (URL, 哈希) 绑定；
    - 同一内容重复提交为更新；原文变化后哈希改变，旧记录不再命中；
    - 详情页无法读取时返回 502，不产生任何纠偏记录。
    """
    try:
        html = await fetch_detail_html(request.detail_url)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "detail_url": request.detail_url,
                "reason": f"详情页读取失败：{describe_error(exc)}",
            },
        ) from exc

    digest = content_hash(html)
    record = CorrectionRecord(
        detail_url=request.detail_url,
        content_hash=digest,
        road_id=request.road_id.strip(),
        direction=request.direction.strip(),
        effective_start=request.effective_start,
        effective_end=request.effective_end,
        weekdays=request.weekdays,
        clock_start=request.clock_start,
        clock_end=request.clock_end,
        cross_midnight=request.resolved_cross_midnight(),
        title=extract_title(html),
    )
    saved = get_store().upsert(record)
    assert saved.created_at is not None and saved.updated_at is not None
    return CorrectionResponse(
        detail_url=saved.detail_url,
        content_hash=saved.content_hash,
        road_id=saved.road_id,
        direction=saved.direction,
        effective_start=saved.effective_start,
        effective_end=saved.effective_end,
        weekdays=saved.weekdays,
        clock_start=saved.clock_start,
        clock_end=saved.clock_end,
        cross_midnight=saved.cross_midnight,
        title=saved.title,
        created_at=saved.created_at,
        updated_at=saved.updated_at,
    )
