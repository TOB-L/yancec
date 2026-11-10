# 运维与运行台账

> 这一层解决一个很具体的问题：出了问题时，能不能回答"这次运行到底发生了什么"。

## 1. 运行台账

每次运行追加一条记录（`backend/data/run_stats.jsonl`），字段固定：

```jsonc
{
  "run_id": "20260919_142530_ab12cd",
  "engine_version": "v0.9.90",
  "prompt_set": "72c514f9",
  "assumption_set": "5e1c0a77",
  "question_hash": "…",
  "depth": "deep",              // free / standard / deep / custom
  "status": "success",          // success / partial / failed / stopped
  "elapsed_sec": 573.4,
  "tokens": { "prompt": 0, "completion": 0, "total": 245000 },
  "calls": 96,                  // 工具调用总次数
  "routes": 12,                 // 实际发出的源族数
  "evidence": 63,               // 证据条数
  "gates": { "pass": 0, "warn": 0, "block": 11 },
  "rework": 1,
  "reused_asset": true,
  "created_at": "2026-09-19T14:25:30+08:00"
}
```

台账只追加，不覆盖。**测试运行必须走离线开关**（`YANCEC_OFFLINE=1`），否则会把看板分母撑大——这个坑真实发生过：反复跑自测，把账本从 6 行撑到 10 行，复购看板的分母跟着虚高。

## 2. 积分与预算

- 每次运行前预估消耗区间，超出预算直接停询，不"先跑了再说"；
- 返工有上限（默认 2 轮），超过则带病披露，不无限重试；
- 预算停止与质检停止分开记账，不合并成"系统主动停止"。

## 3. 版本与水印

- 引擎版本只有一个出处：`core/version.py`；
- 报告头部、`/health`、审计包 `manifest.json` 都从这里取；
- 修改代码后**必须重启进程**再验证。热重载不会让已加载的模块换新——这个坑也踩过：改完 `debate.py` 落盘，下一跑仍是旧代码、旧指纹，直到重启才生效。

排查顺序：

```bash
ls -l backend/core/debate.py                 # 看 mtime
curl -s localhost:8000/health | jq .started_at
# started_at 必须晚于 mtime，否则跑的还是旧进程
```

## 4. 快照与冻结

一次"可用于对外引用"的运行，需要固定四件事：

1. 报告的 `run_id` 与导出件哈希；
2. `prompt_set` / `assumption_set` / `judge_prompt_fp`；
3. 审计包与报告同刻产出；
4. 前端截图来自同一快照。

四项不齐，不进入对外材料。理由很简单：任何一项漂移，复核者都会得出"你说的和跑出来的不是一个东西"。

## 5. AI 生成内容标识

`core/ai_label.py` 负责写入法定隐式标识：

- Word：`dc:creator` / `cp:lastModifiedBy` / `dc:description` / `cp:keywords` / `cp:category`；
- PDF：LibreOffice 不会映射 `dc:description`，需用 `pypdf` 补 `/Author` `/Title` `/Subject` `/Keywords`；
- 内容编号 = 正文 SHA-256 前 16 位，前缀 `AIGC-YC-`。

注意：**手改正文会让编号失效**，而导出元数据仍带旧编号。对外材料若手工改过，必须在答疑页注明"演示件含人工标注"，或者重新生成。

## 6. 离线模式

未配置 Key、或显式设置 `YANCEC_OFFLINE=1` 时：

- 检索走内置样例，模型走模板；
- 报告头部写明"本次为离线演示，以下层未真实执行"；
- 不写生产账本。

离线模式是演示保底，不是"假装跑过"。任何降级都必须在交付物上留痕。

## 7. 常见故障

| 现象 | 先查 | 处理 |
|---|---|---|
| 改了代码没生效 | 进程启动时间 vs 文件 mtime | 重启后端 |
| 报告数字与台账不一致 | `run_id` 是否同一跑 | 以同刻审计包为准 |
| 来源全是同一家 | 注册表健康探针、`degraded` 计数 | 换通道或补直取源 |
| 闸门不 BLOCK | 决策区定义、规则命中位置 | 先看 `findings.where` |
| 导出 PDF 中文乱码 | 字体是否嵌入 | 用 LibreOffice 转，检查字体 |
| 前端读数为 0 | 后端事件是否发出 | 查 SSE 日志，不要改前端兜底 |

## 8. 日志

- 统一输出到 stderr，可重定向到 `backend.err.log`；
- 关键事件：路由决策、通道降级、URL 被拦、闸门判定、返工触发；
- 不记录 API Key、完整用户输入与任何个人信息。

```bash
uvicorn app:app --host 127.0.0.1 --port 8000 2> backend.err.log
```
