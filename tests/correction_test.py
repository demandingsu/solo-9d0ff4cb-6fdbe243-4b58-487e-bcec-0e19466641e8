"""纠偏流程端到端冒烟测试（仅标准库）。

覆盖需求：
1. 原文无法自动解析时，详情按采集失败处理 -> undetermined；
2. POST /corrections 读取当前详情并以内容 SHA-256 为条件保存核正字段；
3. /check 重新采集时，同一内容使用核正字段（即使原文无法自动解析）；
4. 详情正文变化后旧纠偏失效：返回待复核地址与 undetermined，
   不采信未经复核的解析结果声明畅通；
5. 待复核期间，其他公告已确认的受阻路段仍保留；
6. 对新内容重新核正后恢复正常判定。

前置：API 在 127.0.0.1:8000、样本站在 127.0.0.1:8080。
"""
import json
import urllib.error
import urllib.request

API = "http://127.0.0.1:8000"
SITE = "http://127.0.0.1:8080"
LIST_URL = f"{SITE}/announcements?page=1"
DETAIL_URL = f"{SITE}/announcement/gg-2026-004"


def request(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def admin(method, path):
    req = urllib.request.Request(SITE + path, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def check(segments):
    _, data = request("POST", "/check", {
        "announcement_list_url": LIST_URL, "segments": segments})
    return data


# 周五 22:00 驶入 G45-K77 下行，撞上核正后的 21:00-23:30（每周五）
SEG_HIT = {
    "road_id": "G45-K77", "direction": "下行",
    "enter_at": "2026-11-20T22:00:00+08:00",
    "exit_at": "2026-11-20T23:00:00+08:00",
}
# 周六中午，不与任何 G45-K77 封闭窗口相交
SEG_MISS = {
    "road_id": "G45-K77", "direction": "下行",
    "enter_at": "2026-11-21T12:00:00+08:00",
    "exit_at": "2026-11-21T13:00:00+08:00",
}
# G25-K88 周六全天封闭，用于验证“其他公告已确认受阻仍保留”
SEG_OTHER = {
    "road_id": "G25-K88", "direction": "上行",
    "enter_at": "2026-11-21T06:00:00+08:00",
    "exit_at": "2026-11-21T09:00:00+08:00",
}

CORRECTION = {
    "detail_url": DETAIL_URL,
    "road_id": "G45-K77",
    "direction": "下行",
    "effective_start": "2026-11-10",
    "effective_end": "2026-12-10",
    "weekdays": [4],  # 周五
    "clock_start": "21:00",
    "clock_end": "23:30",
}

# 1. 恢复样本站默认状态，清理可能残留的运行状态
admin("POST", "/admin/reset")
print("== 1. 默认 normal 版本可被自动解析 ==")
d = check([SEG_MISS])
print("  status:", d["status"], "| fetched:", d["fetched_announcements"])
assert d["fetched_announcements"] == 4
assert d["pending_review"] == []

# 2. 切到 garbled：原文无法自动解析，该详情登记采集失败
print("== 2. garbled 正文无法自动解析 -> 采集失败/undetermined ==")
admin("POST", "/admin/announcement/gg-2026-004/variant/garbled")
d = check([SEG_HIT])
print("  status:", d["status"], "| failures:", [f["url"] for f in d["failures"]])
assert d["status"] == "undetermined"
assert any(f["stage"] == "detail" and f["url"] == DETAIL_URL for f in d["failures"])
assert d["clear"] == []
assert d["blocked"] == []  # 该公告未解析出，不能凭乱码文本判定受阻

# 3. 提交人工纠偏：服务实时读取详情并绑定内容 SHA-256
print("== 3. 提交人工纠偏（以内容 SHA-256 为条件） ==")
status, resp = request("POST", "/corrections", CORRECTION)
print("  http:", status, "| hash:", resp["content_hash"][:16], "...")
assert status == 201
assert len(resp["content_hash"]) == 64
assert resp["cross_midnight"] is False
assert resp["road_id"] == "G45-K77"
hash_garbled = resp["content_hash"]

# 重复提交同一内容：更新而非新建，哈希不变
status, resp2 = request("POST", "/corrections", CORRECTION)
assert status == 201 and resp2["content_hash"] == hash_garbled
assert resp2["created_at"] == resp["created_at"]
assert resp2["updated_at"] >= resp["updated_at"]

# 4. 同内容重新采集：即使原文无法自动解析，也必须使用核正字段
print("== 4. 同内容重新采集 -> 套用核正字段（无需自动解析） ==")
d = check([SEG_HIT])
print("  status:", d["status"], "| fetched:", d["fetched_announcements"],
      "| blocked:", [b["road_id"] for b in d["blocked"]])
assert d["fetched_announcements"] == 4  # garbled 详情经纠偏恢复为有效公告
assert not any(f["url"] == DETAIL_URL for f in d["failures"])
hit = next(b for b in d["blocked"] if b["road_id"] == "G45-K77")
assert hit["blocking"][0]["start"] == "2026-11-20T21:00:00+08:00"
assert hit["blocking"][0]["end"] == "2026-11-20T23:30:00+08:00"
assert hit["announcements"] == [DETAIL_URL]

# 样本站仍带失效链接，整体仍 undetermined，但受阻结论保留
assert d["status"] == "undetermined"
assert any(b["road_id"] == "G45-K77" for b in d["blocked"])

# 5. 原站改版（正文变化）：旧纠偏失效 -> 待复核，不采信自动解析
print("== 5. 正文变化 -> 旧纠偏失效、待复核、undetermined ==")
admin("POST", "/admin/announcement/gg-2026-004/variant/revised")
d = check([SEG_MISS, SEG_OTHER])
print("  status:", d["status"], "| pending:", d["pending_review"])
assert d["status"] == "undetermined"
assert len(d["pending_review"]) == 1
assert d["pending_review"][0]["url"] == DETAIL_URL
# revised 其实可自动解析，但未经复核绝不能拿来声明畅通
assert d["clear"] == []
# 关键安全语义：不得用未经复核的解析结果声明畅通
assert not any(b["road_id"] == "G45-K77" for b in d["blocked"])
# 其他公告已确认的受阻路段仍保留
assert any(b["road_id"] == "G25-K88" for b in d["blocked"])
# 旧纠偏对应的失败不再以 failure 形式出现，而是 pending_review
assert not any(f["url"] == DETAIL_URL for f in d["failures"])

# 6. 对新内容重新核正：待复核消除，按新字段判定
print("== 6. 对新内容重新核正 -> 恢复判定 ==")
new_correction = dict(CORRECTION)
new_correction.update({
    "direction": "上行",
    "effective_start": "2026-11-20",
    "effective_end": "2026-12-31",
    "weekdays": [1, 5],  # 周二、周六
    "clock_start": "07:00",
    "clock_end": "09:30",
})
status, resp3 = request("POST", "/corrections", new_correction)
assert status == 201
assert resp3["content_hash"] != hash_garbled, "正文变化后哈希必须不同"
d = check([SEG_OTHER])
print("  status:", d["status"], "| pending:", d["pending_review"],
      "| blocked:", [b["road_id"] for b in d["blocked"]])
assert d["pending_review"] == []
assert any(b["road_id"] == "G25-K88" for b in d["blocked"])

# 新核正字段生效：周六 08:00 上行与 07:00-09:30 相交
seg_revised_hit = {
    "road_id": "G45-K77", "direction": "上行",
    "enter_at": "2026-11-21T08:00:00+08:00",
    "exit_at": "2026-11-21T09:00:00+08:00",
}
d = check([seg_revised_hit])
assert any(b["road_id"] == "G45-K77" for b in d["blocked"])
hit = next(b for b in d["blocked"] if b["road_id"] == "G45-K77")
assert hit["blocking"][0]["start"] == "2026-11-21T07:00:00+08:00"

# 7. 参数校验与错误处理
print("== 7. 入参校验与详情读取失败 ==")
bad = dict(CORRECTION)
bad["weekdays"] = [7]
status, err = request("POST", "/corrections", bad)
assert status == 422, err
bad2 = dict(CORRECTION)
bad2["detail_url"] = "not-a-url"
status, err = request("POST", "/corrections", bad2)
assert status == 422, err
bad3 = dict(CORRECTION)
bad3["detail_url"] = f"{SITE}/announcement/gg-2026-missing"
status, err = request("POST", "/corrections", bad3)
assert status == 502 and err["detail"]["detail_url"] == bad3["detail_url"]
print("  422/502 错误处理正常")

# 8. 恢复样本站默认状态
admin("POST", "/admin/reset")
print("\n全部纠偏断言通过 ✔")
