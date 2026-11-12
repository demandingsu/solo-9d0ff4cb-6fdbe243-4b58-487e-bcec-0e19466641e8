"""Pydantic 模型：请求路段、纠偏请求与响应结构。"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# 整体判定状态：
# - clear          全部公告均成功采集，且所有路段均无相交封闭
# - blocked        存在受阻路段（不代表全部路段受阻）
# - undetermined   存在采集失败，无法给出完整判定（绝不会降级为“畅通”）
CheckStatus = Literal["clear", "blocked", "undetermined"]


class RoadSegment(BaseModel):
    """调度计划中的一个路段通行区间。"""

    road_id: str = Field(..., description="路段编号，如 G15-K123")
    direction: str = Field(..., description="通行方向，如 上行/下行；与公告方向做归一化匹配")
    enter_at: datetime = Field(..., description="带时区的驶入时间（ISO 8601）")
    exit_at: datetime = Field(..., description="带时区的驶出时间（ISO 8601）")

    @field_validator("enter_at", "exit_at")
    @classmethod
    def _must_be_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("时间必须带时区，例如 2026-11-16T21:00:00+08:00")
        return v

    @field_validator("exit_at")
    @classmethod
    def _exit_after_enter(cls, v: datetime, info) -> datetime:
        enter = info.data.get("enter_at")
        if enter is not None and v <= enter:
            raise ValueError("驶出时间必须晚于驶入时间")
        return v


class CheckRequest(BaseModel):
    """采集与核对请求。"""

    announcement_list_url: str = Field(..., description="公告列表页 URL")
    segments: list[RoadSegment] = Field(..., min_length=1)


class BlockingInterval(BaseModel):
    """与通行区间相交的一段封闭区间（北京时间，带 +08:00 偏移）。"""

    start: datetime
    end: datetime


class BlockedResult(BaseModel):
    """单个受阻路段的命中明细。"""

    road_id: str
    direction: str
    enter_at: datetime
    exit_at: datetime
    blocking: list[BlockingInterval]
    announcements: list[str] = Field(
        default_factory=list, description="命中的公告详情页链接（去重）"
    )


class ClearResult(BaseModel):
    """未命中封闭的路段（仅在全部采集成功时给出）。"""

    road_id: str
    direction: str
    enter_at: datetime
    exit_at: datetime


class FetchFailure(BaseModel):
    """采集失败记录：标记无法判定并给出失败地址。"""

    url: str
    stage: Literal["list", "detail"]
    reason: str


class PendingReview(BaseModel):
    """待复核：旧纠偏所绑定的内容哈希已与当前页面不符（原文已变化）。

    此时既不套用旧纠偏，也不采信未经复核的自动解析，必须人工重新核正。
    """

    url: str
    reason: str


class CheckResponse(BaseModel):
    status: CheckStatus
    timezone_note: str = "公告时间均按北京时间(UTC+8)解释"
    fetched_announcements: int
    blocked: list[BlockedResult] = Field(default_factory=list)
    clear: list[ClearResult] = Field(default_factory=list)
    failures: list[FetchFailure] = Field(default_factory=list)
    pending_review: list[PendingReview] = Field(default_factory=list)
    message: Optional[str] = None


class CorrectionRequest(BaseModel):
    """人工纠偏：提交详情 URL 与核正后的封闭字段。

    服务会实时读取该详情页，以页面内容的 SHA-256 作为记录生效条件。
    """

    detail_url: str = Field(..., description="需要核正的公告详情页 URL")
    road_id: str = Field(..., min_length=1, description="核正后的路段编号")
    direction: str = Field(..., min_length=1, description="核正后的方向，如 上行/下行/双向")
    effective_start: date = Field(..., description="核正后的生效开始日")
    effective_end: Optional[date] = Field(None, description="生效结束日；留空表示长期有效")
    weekdays: list[int] = Field(
        ..., min_length=1, description="每周封闭日：0=周一 … 6=周日"
    )
    clock_start: time = Field(..., description="核正后的每日封闭开始钟点 HH:MM")
    clock_end: time = Field(..., description="核正后的每日封闭结束钟点 HH:MM")
    cross_midnight: Optional[bool] = Field(
        None,
        description=(
            "是否跨午夜（封闭归属开始当天）。留空时自动推断："
            "结束钟点不晚于开始钟点即视为跨午夜"
        ),
    )

    @field_validator("detail_url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("detail_url 必须是 http/https 绝对地址")
        return v

    @field_validator("weekdays")
    @classmethod
    def _weekday_range(cls, v: list[int]) -> list[int]:
        bad = [d for d in v if not 0 <= d <= 6]
        if bad:
            raise ValueError("封闭日取值范围为 0(周一) 到 6(周日)")
        if len(set(v)) != len(v):
            raise ValueError("封闭日存在重复项")
        return sorted(v)

    @field_validator("effective_end")
    @classmethod
    def _end_after_start(cls, v: Optional[date], info) -> Optional[date]:
        start = info.data.get("effective_start")
        if v is not None and start is not None and v < start:
            raise ValueError("生效结束日不能早于开始日")
        return v

    def resolved_cross_midnight(self) -> bool:
        if self.cross_midnight is not None:
            return self.cross_midnight
        return self.clock_end <= self.clock_start


class CorrectionResponse(BaseModel):
    """纠偏写入结果：回显核正字段及其绑定的内容哈希。"""

    detail_url: str
    content_hash: str = Field(..., description="提交时详情页正文的 SHA-256（hex）")
    road_id: str
    direction: str
    effective_start: date
    effective_end: Optional[date]
    weekdays: list[int]
    clock_start: time
    clock_end: time
    cross_midnight: bool
    title: str
    created_at: datetime
    updated_at: datetime
