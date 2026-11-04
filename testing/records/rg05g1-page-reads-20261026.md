# RG-05g1 有界读取接口：交付与验证记录

日期：2026-10-26。契约：[K02](../../specs/01-evidence-to-report/plan.md#k02-page-reads)；任务卡：tasks.md「3／RG-05g1 有界读取接口」。

## 一、改动文件

| 文件 | 改动 |
| --- | --- |
| `src/dd_agent/page_queries.py` | 新增。`PageReader`（目录／单页／历史）＋`PageQueryError` 领域错误＋K02 错误映射表；不依赖 web 框架。 |
| `src/dd_agent/db.py` | 新增 `reading_version`、`page_catalog`、`page_row`、`page_history`、`page_image_key`；全部为单页／单批 SQL。 |
| `src/dd_agent/app.py` | 注册 `page-catalog`、`pages/{number}`、`pages/{number}/history`；`PageQueryError` 统一转状态码；预览图改单页定位。 |

未改前端、未改迁移、未改模型 worker。

## 二、接口

前缀均为 `/api/projects/{project_id}/documents/{document_id}`。

### 1) `GET /page-catalog`

参数：`limit`（默认 50，最大 100）、`after`（页号 keyset 游标，≥0，可空）。

```json
{
  "document_id": "…", "reading_version": "pdfium-text-v2-…-render2",
  "page_count": 649, "total_pages": 649, "succeeded_pages": 649, "failed_pages": 0,
  "status_revision": "…", "content_revision": "…", "as_of": "2026-10-26T16:05:00+08:00",
  "snapshot": "not_frozen",
  "items": [
    {"page_number": 600, "status": "succeeded", "method": "ocr", "error_code": "",
     "verification": "unverified", "source_revision": "…", "has_image": true}
  ],
  "next_cursor": 650
}
```

- 不含正文、行框、`image_key` 或内部存储路径；条目字段固定为 7 个。
- `next_cursor` 为最后一条的页号；无下一批时为 `null`。
- `status_revision` 随页状态／进度变化；`content_revision` 只由成功页的来源版本决定，**纯进度变化不改它**。
- `snapshot: not_frozen` 明示非冻结快照：同阅读版本下新完成页会出现在后续批次，不必让已有游标失效。

### 2) `GET /pages/{number}`

参数：`view=raw|effective`（默认 effective）、`offset`（≥0，默认 0）、`source_revision`（续读绑定，≤64 字符）、`include_lines`（默认 false）。

```json
{
  "page_number": 600, "view": "effective",
  "status": "succeeded", "method": "ocr", "error_code": "",
  "revision": "…", "raw_revision": "…", "source_revision": "…", "verification": "unverified",
  "text": "……", "total_length": 250000, "offset": 0, "next_offset": 100000, "slice_limit": 100000,
  "has_image": true,
  "image_url": "/api/projects/…/documents/…/pages/600/image"
}
```

- 单次最多返回 100,000 字符；`next_offset` 非空时带 `offset=next_offset&source_revision=revision` 续读。
- 续读绑定：`source_revision` 与当前 `revision` 不一致返回 409 `stale`；`offset` 超过正文长度同样 409。
- `view=raw` 的有效来源就是基础阅读行，故 `revision` 取 `raw_revision`；行框只在 `include_lines=true` 时返回。

### 3) `GET /pages/{number}/history`

参数：`limit`（默认 10，最大 50）、`after`（游标 `创建时间|记录ID`）。

```json
{
  "page_number": 1, "raw_revision": "…",
  "items": [{"created_at": "2026-10-26T15:00:00+08:00", "profile_version": "profile-1",
             "base_revision": "…", "verification": "machine_checked", "is_current": true,
             "text_length": 600, "text_preview": "……", "preview_truncated": true}],
  "next_cursor": "2026-10-26T15:00:00+08:00|…"
}
```

- 最新状态在单页接口，这里只列该页机器增强历史；正文只给 500 字符预览并显式标记 `preview_truncated`。
- `is_current` 表示该次增强的 `base_revision` 仍等于当前基础修订，只有它为真的那条是当前有效文字。

### 4) 预览图

`GET /pages/{number}/image` 保留原路径与错误文案，改为单页 SQL 定位（`page_image_key`），不再先取回整卷页记录再筛选。

## 三、错误映射（K02）

| code | HTTP | 触发 |
| --- | --- | --- |
| `not_found` | 404 | 页号超出页数、材料不存在 |
| `not_processed` | 409 | 尚无阅读版本、该页尚未读取 |
| `stale` | 409 | 续读的 `source_revision` 或偏移与当前不符 |
| `missing_artifact` / `wrong_project` / `bounded_read_required` | 404／404／409 | 预留给缺产物、跨项目适配器与旧无界接口收口 |
| `busy` / `timeout` | 503／504 | 连接繁忙、超时 |

跨项目一律返回 404 与同一句「材料不存在或不属于当前项目。」，不回显材料或对方项目 ID；`wrong_project` 目前不对外区分，避免用 UUID 探测。

## 四、有界口径

| 项 | 默认 | 上限 | 超出时 |
| --- | --- | --- | --- |
| 每批页数 | 50 | 100 | 422 |
| 单页正文字符 | — | 100,000／次 | 返回 `next_offset` 续读，不静默截断 |
| 历史条数 | 10 | 50 | 422 |
| 历史预览 | 500 字符／条 | — | 标记 `preview_truncated` |

## 五、查询行数／SQL 次数对照

`PageReader.page(600)` 在 649 页与 20 页快照下的计数**完全相同**（`sql.calls` 与 `sql.fetched_rows` 相等）：材料边界 2 条＋阅读版本 1 条＋单页 1 条，另加连接归还时的 3 条会话清理语句；`sql.fetched_rows ≤ 6`。预览图定位 `sql.fetched_rows = 1`。目录每批恒定 2 条查询（聚合＋本批元数据），最多取 `limit+1` 行。

## 六、测试结果

`testing/tests/test_page_reads.py`：**8 passed**。覆盖 20／200／649 页合成快照、深链接第 600 页、目录 keyset 续读与字段封闭、单页正文切片与来源绑定续读、raw／effective 与按需行框、跨项目与未处理错误、历史分页与 `is_current`、`content_revision`／`status_revision` 分离、预览图单页定位。

受影响区域回归（`test_workspace`、`test_material_catalog`、`test_page_reads`、`test_reading`、`test_automatic`、`test_retrieval`、`test_review`、`test_performance`）：**78 passed，2 skipped**；其余 7 个测试文件（`test_cache`、`test_db_connections`、`test_db_connections_baseline`、`test_pause_resume`、`test_recovery`、`test_performance_baseline`、`test_harness`）：**41 passed**。合计 15 个测试文件 **119 passed／2 skipped**（2 项为默认未开启的真实模型测试）。

## 七、局限与后续

- 旧的无界 `/pages` 接口仍原样保留：K02 要求「先新增兼容，再由 g2 切换，最后限制旧接口」。收口时对超出安全范围的材料返回 409 `bounded_read_required` 并指向新入口，届时由 g2 一并处理。
- 目录游标是页号 keyset，不携带阅读版本；版本切换后客户端应以响应里的 `reading_version` 为准重置，不做跨版本拼接。
- `bounded_read_required` 与 `wrong_project` 两个错误码已进入映射表但当前没有触发点，属预留。
