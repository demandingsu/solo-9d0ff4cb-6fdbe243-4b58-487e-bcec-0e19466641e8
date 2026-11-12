# 冷链配送施工封闭核对 API

出车前核对计划路线是否与公路施工封闭冲突。给定公告列表页 URL 和计划路段
（编号、方向、带时区的驶入/驶出时间），服务会：

1. 抓取公告列表页并自动跟随**同站“下一页”分页**；
2. 逐页抓取公告详情（BeautifulSoup 解析），提取**路段、方向、生效日期、
   每周封闭日、起止钟点**；
3. 按**路段编号 + 方向**匹配，判断路段通行区间与封闭区间是否相交；
4. 返回受阻路段、相交的封闭时段与公告链接；
5. 任何页面抓取失败时标记 `undetermined`（无法判定）并给出失败地址，
   **绝不会在信息不完整时报“全程畅通”**；
6. 对被采集器误读的公告，资料员可通过 **`POST /corrections` 人工纠偏**：
   提交详情 URL 与核正后的字段，服务实时读取详情并以**正文 SHA-256**
   为条件在 SQLite 保存；`/check` 重新采集时同一内容直接套用核正字段，
   原文变化后旧纠偏自动失效并把该地址列入**待复核**。

## 时间规则（需求约定）

- 公告中的所有时间一律按**北京时间（UTC+8）**解释；请求中的时间可带任意
  时区偏移（如 `Z`、`+09:00`），服务内部统一换算为北京时间。
- **跨午夜封闭归属开始当天**：如 `22:00 至 次日 05:00` 的封闭日按 22:00
  所在日期计算，结束时刻落在次日。
- **生效日期限定封闭开始日**：只有封闭区间的开始日落入生效日期区间内，
  该封闭才成立；跨午夜窗口的尾部即使延伸到生效期外也不影响归属。
- 通行区间与封闭区间均按半开区间 `[start, end)` 判断相交，恰好首尾相接
  （如 05:00 驶入、封闭 05:00 结束）不算受阻。
- `00:00 至 24:00` 视为当天全天封闭（跨午夜到次日 00:00）。

## 目录结构

```
.
├── app/                  # FastAPI 采集与核对服务
│   ├── main.py           # 路由：GET /health、POST /check、POST /corrections
│   ├── models.py         # 请求/响应模型（时间必须带时区）
│   ├── parser.py         # BeautifulSoup 列表分页与详情解析、内容 SHA-256
│   ├── scraper.py        # httpx 异步抓取、重试、失败/待复核记录、纠偏套用
│   ├── scheduler.py      # 路段匹配与区间相交判定
│   ├── corrections.py    # 人工纠偏 SQLite 存储（按 URL+内容哈希绑定）
│   └── timeutils.py      # 北京时间、跨午夜、生效期、星期解析
├── sample-site/          # 离线样本站（Flask，同构分页+详情，含失效链接与
│                         #   详情正文版本切换 /admin 接口）
├── tests/smoke_test.py       # 端到端冒烟测试（仅标准库）
├── tests/correction_test.py # 纠偏/待复核流程端到端测试（仅标准库）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 用 Docker Compose 启动（离线可用）

镜像构建仅需在有网络时拉取基础镜像与依赖；运行后两个容器之间走内部
Docker 网络，调用样本站不需要外网。

```bash
docker compose up --build
```

- API 文档：<http://localhost:8000/docs>
- 样本站列表：<http://localhost:8080/announcements?page=1>

## 请求示例

```bash
curl -X POST http://localhost:8000/check \
  -H 'Content-Type: application/json' \
  -d '{
    "announcement_list_url": "http://sample-site:8080/announcements?page=1",
    "segments": [
      {
        "road_id": "G15-K123",
        "direction": "下行",
        "enter_at": "2026-11-16T21:30:00+08:00",
        "exit_at":  "2026-11-17T02:00:00+08:00"
      },
      {
        "road_id": "G25-K88",
        "direction": "上行",
        "enter_at": "2026-11-14T06:00:00+08:00",
        "exit_at":  "2026-11-14T09:00:00+08:00"
      }
    ]
  }'
```

> 容器内访问样本站用 `http://sample-site:8080`；从宿主机访问用
> `http://localhost:8080`（此时把该 URL 作为 `announcement_list_url` 即可）。

## 响应说明

| 字段 | 含义 |
| --- | --- |
| `status` | `clear` 全部采集成功且无相交；`blocked` 存在受阻路段；`undetermined` 有采集失败或待复核地址，无法给出完整判定 |
| `blocked[]` | 受阻路段、相交封闭时段（北京时间）、命中的公告详情链接 |
| `clear[]` | 未受阻路段；**仅当零采集失败且零待复核时才会出现** |
| `failures[]` | 采集失败地址、阶段（`list`/`detail`）与原因（如 `HTTP 404`、超时） |
| `pending_review[]` | 内容已变化导致旧纠偏失效、等待人工重新核正的详情地址 |
| `fetched_announcements` | 成功解析或经纠偏恢复的公告条数 |

样本站故意包含一个已撤销公告的失效链接，因此对样本站调用时 `status`
会是 `undetermined`，同时 `failures` 中给出 404 地址——用于演示失败处理。

## 人工纠偏（POST /corrections）

当施工公告的路段或封闭时段被采集器误读、原站尚未更正时，资料员提交核正字段：

```bash
curl -X POST http://localhost:8000/corrections \
  -H 'Content-Type: application/json' \
  -d '{
    "detail_url": "http://sample-site:8080/announcement/gg-2026-004",
    "road_id": "G45-K77",
    "direction": "下行",
    "effective_start": "2026-11-10",
    "effective_end": "2026-12-10",
    "weekdays": [4],
    "clock_start": "21:00",
    "clock_end": "23:30"
  }'
```

- `weekdays`：`0=周一 … 6=周日`，可传多个；`effective_end` 留空表示长期有效；
- `cross_midnight` 可省略，结束钟点不晚于开始钟点时自动判定为跨午夜
  （封闭归属开始当天），也可显式传 `true/false`。

### 纠偏生效与失效规则（以内容 SHA-256 为条件）

1. 提交时服务**实时抓取** `detail_url`，对响应正文计算 SHA-256，纠偏记录
   以 `(detail_url, content_hash)` 为主键存入 SQLite；详情页读取失败返回
   `502`，不产生记录。
2. 之后 `/check` 重新采集到**同一内容**（哈希一致）时，直接套用核正字段，
   **即使原文无法自动解析**也能正常参与路段核对。
3. 原站更正/改版导致正文变化后哈希不同：旧纠偏**自动失效**，该详情地址
   进入响应的 `pending_review`，整体状态为 `undetermined`；此时既不套用
   旧字段，也**不采信未经复核的自动解析结果**（不会据此声明畅通）。
4. 待复核不影响其他公告：其他公告已确认的受阻路段仍保留在 `blocked`。
5. 对新内容重新调用 `POST /corrections` 核正后，待复核消除，按新字段判定。
6. 同一 `(URL, 哈希)` 重复提交为更新（保留首次创建时间，刷新更新时间）。

纠偏库路径由环境变量 `CORRECTION_DB_PATH` 控制，默认 `data/corrections.db`；
Compose 部署时位于命名卷 `correction-data` 的 `/data/corrections.db`，
容器重启后记录保留。

## 样本站正文版本切换（离线联调）

样本站的 `gg-2026-004` 公告内置三种详情正文，用于复现“误读 → 纠偏 →
原站改版 → 待复核”的完整链路：

| 版本 | 特征 |
| --- | --- |
| `normal`（默认） | 字段齐全，可自动解析 |
| `garbled` | 正文残缺/口语化（缺路段编号、钟点非 HH:MM），自动解析必然失败 |
| `revised` | 原站改版后的另一套字段，正文与哈希均不同 |

```bash
curl -X POST http://localhost:8080/admin/announcement/gg-2026-004/variant/garbled
curl -X POST http://localhost:8080/admin/announcement/gg-2026-004/variant/revised
curl      http://localhost:8080/admin/state
curl -X POST http://localhost:8080/admin/reset
```

## 本地开发（不用 Docker）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r sample-site/requirements.txt
python sample-site/app.py &
CORRECTION_DB_PATH=data/corrections.db uvicorn app.main:app --port 8000
python tests/smoke_test.py
python tests/correction_test.py
```

## 解析约定

- 列表页：提取同站（同 scheme + 同 host）的公告详情链接，并按“下一页/
  next/›”等文本识别分页链接，最多跟随 50 页并按 URL 去重防环。
- 详情页：从含“路段编号/方向/生效日期/封闭日/封闭时间”关键词的行中用
  正则提取字段；字段缺失或无法解析时该详情页按采集失败处理。
- **纠偏优先**：详情抓取成功后先算正文 SHA-256，命中纠偏记录则直接使用
  人工核正字段（跳过自动解析）；有历史纠偏但哈希不符则转待复核；
  两者都没有时才走自动解析。
- 方向：公告为“双向”时匹配任一通行方向；否则需精确一致（忽略空白）。
- 请求超时 10 秒（连接 5 秒），每个地址失败后重试 1 次。
