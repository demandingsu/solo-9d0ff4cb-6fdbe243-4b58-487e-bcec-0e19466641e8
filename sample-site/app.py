"""离线样本站：模拟公路施工封闭公告的同站分页列表与详情页。

数据全部内置，无外部依赖，供采集 API 离线联调。
页面结构与真实公告站点同构：
- /announcements?page=N  列表页（含“下一页”同站分页）
- /announcement/<id>     详情页（路段/方向/生效日期/每周封闭日/起止钟点）
- 故意提供一个失效链接，用于验证“采集失败 -> 无法判定”的处理。

详情正文可切换版本（模拟原站更正/改版，正文字节变化导致内容哈希变化）：
- POST /admin/announcement/<id>/variant/<name>  切换详情正文版本
- GET  /admin/state                              查看当前版本
- POST /admin/reset                              恢复默认版本

内置可切换公告 gg-2026-004 提供三个版本：
- normal  ：字段齐全、可自动解析（默认）
- garbled ：正文被误读/残缺，采集器无法自动解析（缺路段编号、钟点非 HH:MM）
- revised ：原站改版后的另一正文，字段与 garbled 完全不同（哈希随之变化）
"""
from __future__ import annotations

from datetime import date
from threading import Lock

from flask import Flask, abort, jsonify, render_template_string, request

app = Flask(__name__)

LIST_TEMPLATE = """
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>施工封闭公告列表</title></head>
<body>
<h1>公路施工封闭公告</h1>
<table class="announcement-table">
  <thead><tr><th>公告标题</th><th>发布日期</th></tr></thead>
  <tbody>
  {% for item in items %}
    <tr>
      <td><a href="{{ item.href }}">{{ item.title }}</a></td>
      <td>{{ item.published }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
<div class="pagination">
  {% if page > 1 %}<a rel="prev" href="/announcements?page={{ page - 1 }}">上一页</a>{% endif %}
  {% if has_next %}<a rel="next" href="/announcements?page={{ page + 1 }}">下一页</a>{% endif %}
</div>
</body></html>
"""

DETAIL_TEMPLATE = """
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{{ ann.title }}</title></head>
<body>
<article class="content">
  <h1>{{ ann.title }}</h1>
  {% for line in ann.lines %}
  <p>{{ line[0] }}：{{ line[1] }}</p>
  {% endfor %}
  <p>管制措施：施工期间该路段禁止车辆通行，请提前绕行。</p>
</article>
</body></html>
"""

ANNOUNCEMENTS = [
    {
        "id": "gg-2026-001",
        "title": "G15沈海高速K123段下行夜间施工封闭公告",
        "published": "2026-11-01",
        "variants": {
            "normal": {
                "lines": [
                    ("路段编号", "G15-K123"),
                    ("方向", "下行"),
                    ("生效日期", "2026年11月1日至2026年11月30日"),
                    ("封闭日", "每周一、周三"),
                    ("封闭时间", "22:00 至 次日 05:00（跨午夜，归属当日封闭）"),
                ],
            },
        },
    },
    {
        "id": "gg-2026-002",
        "title": "G15沈海高速K123段上行白天施工封闭公告",
        "published": "2026-11-02",
        "variants": {
            "normal": {
                "lines": [
                    ("路段编号", "G15-K123"),
                    ("方向", "上行"),
                    ("生效日期", "2026年11月5日至2026年11月25日"),
                    ("封闭日", "每周二、周四"),
                    ("封闭时间", "09:00 至 16:30"),
                ],
            },
        },
    },
    {
        "id": "gg-2026-003",
        "title": "G25长深高速K88段双向周末施工封闭公告",
        "published": "2026-11-03",
        "variants": {
            "normal": {
                "lines": [
                    ("路段编号", "G25-K88"),
                    ("方向", "双向"),
                    ("生效日期", "2026年10月15日起长期有效"),
                    ("封闭日", "每周六、周日"),
                    ("封闭时间", "00:00 至 24:00（全天封闭）"),
                ],
            },
        },
    },
    {
        # 可切换正文的公告：默认 normal，可切到 garbled（无法自动解析）
        # 与 revised（原站改版，内容哈希变化，旧纠偏失效）
        "id": "gg-2026-004",
        "title": "G45大广高速K77段夜间施工封闭公告",
        "published": "2026-11-06",
        "variants": {
            "normal": {
                "lines": [
                    ("路段编号", "G45-K77"),
                    ("方向", "下行"),
                    ("生效日期", "2026年11月10日至2026年12月10日"),
                    ("封闭日", "每周五"),
                    ("封闭时间", "21:00 至 23:30"),
                ],
            },
            "garbled": {
                # 模拟采集器误读：路段编号行缺失、钟点为口语化文本，
                # 自动解析必然失败，只能依赖人工纠偏
                "lines": [
                    ("施工位置", "大广高速某处夜间养护（编号待补）"),
                    ("行车方向", "往南方向"),
                    ("管制日期", "十一月十日起到十二月十日"),
                    ("封闭日", "每周封一晚，具体星期见现场告示"),
                    ("封闭时间", "每晚九点开始到夜里十一点半结束"),
                ],
            },
            "revised": {
                # 原站改版后的新正文：字段与 garbled 完全不同，
                # 内容哈希变化，绑定 garbled 的纠偏必须失效、转人工复核
                "lines": [
                    ("路段编号", "G45-K77"),
                    ("方向", "上行"),
                    ("生效日期", "2026年11月20日至2026年12月31日"),
                    ("封闭日", "每周二、周六"),
                    ("封闭时间", "07:00 至 09:30"),
                ],
            },
        },
    },
]

DEFAULT_VARIANT = "normal"
PAGE_SIZE = 2
# 故意放在列表里的失效链接，验证采集失败处理
BROKEN_HREF = "/announcement/gg-2026-deleted"

# 公告 id -> 当前正文版本（内存态，/admin/reset 可恢复）
_state: dict[str, str] = {}
_lock = Lock()


def _all_items() -> list[dict]:
    items = [
        {"href": f"/announcement/{a['id']}", "title": a["title"], "published": a["published"]}
        for a in ANNOUNCEMENTS
    ]
    items.insert(1, {"href": BROKEN_HREF, "title": "【已撤销】临时管制公告", "published": "2026-11-04"})
    return items


def _find(ann_id: str) -> dict | None:
    return next((a for a in ANNOUNCEMENTS if a["id"] == ann_id), None)


def _current_variant(ann_id: str) -> str:
    with _lock:
        return _state.get(ann_id, DEFAULT_VARIANT)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/announcements")
def listing():
    page = max(1, request.args.get("page", default=1, type=int))
    items = _all_items()
    start = (page - 1) * PAGE_SIZE
    page_items = items[start : start + PAGE_SIZE]
    if not page_items and page != 1:
        abort(404)
    has_next = start + PAGE_SIZE < len(items)
    return render_template_string(
        LIST_TEMPLATE, items=page_items, page=page, has_next=has_next
    )


@app.get("/announcement/<ann_id>")
def detail(ann_id: str):
    ann = _find(ann_id)
    if ann is None:
        abort(404)
    variant_name = _current_variant(ann_id)
    variant = ann["variants"].get(variant_name)
    if variant is None:  # 状态异常时退回默认版本
        variant = ann["variants"][DEFAULT_VARIANT]
        variant_name = DEFAULT_VARIANT
    return render_template_string(
        DETAIL_TEMPLATE,
        ann={"title": ann["title"], "lines": variant["lines"]},
    )


@app.post("/admin/announcement/<ann_id>/variant/<variant_name>")
def set_variant(ann_id: str, variant_name: str):
    ann = _find(ann_id)
    if ann is None:
        abort(404)
    if variant_name not in ann["variants"]:
        abort(400, description=f"未知正文版本: {variant_name}")
    with _lock:
        _state[ann_id] = variant_name
    return jsonify({"id": ann_id, "variant": variant_name})


@app.get("/admin/state")
def admin_state():
    with _lock:
        return jsonify({"variants": dict(_state) or {ann["id"]: DEFAULT_VARIANT
                                                      for ann in ANNOUNCEMENTS}})


@app.post("/admin/reset")
def admin_reset():
    with _lock:
        _state.clear()
    return jsonify({"variants": {ann["id"]: DEFAULT_VARIANT for ann in ANNOUNCEMENTS}})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
