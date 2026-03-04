# Colab Multi-Model Service: 落地计划与进度追踪

最后更新：2026-03-04
负责人：待定（默认当前执行 agent）

## 0. 使用说明

1. 本文档是唯一进度事实源（single source of truth）。
2. 每完成一个任务，必须同步更新状态、完成日期、证据链接。
3. 状态枚举：`todo` / `in_progress` / `blocked` / `done`。
4. 若状态为 `blocked`，必须写明阻塞原因和下一步动作。

## 1. 里程碑总览

| Milestone | 目标 | 状态 |
|---|---|---|
| M0 | ImageGen 服务可远程访问并具备基础安全能力 | done |
| M1 | Whisper small 集成与 API 发布 | done |
| M2 | OmniParser 集成与 UI 解析 API 发布 | done |
| M3 | 多能力统一调度与资源治理 | todo |
| M4 | 运维自动化与观测增强 | done |

## 2. 任务分解

### M0（已完成）

| ID | 任务 | 状态 | 完成日期 | 证据 |
|---|---|---|---|---|
| M0-1 | FastAPI image gen 接口上线 | done | 2026-03-04 | `app/main.py` |
| M0-2 | Bearer Token 鉴权 | done | 2026-03-04 | `app/main.py` |
| M0-3 | 前端页面 `/` | done | 2026-03-04 | `app/static/index.html` |
| M0-4 | CORS 支持 | done | 2026-03-04 | `app/main.py` |
| M0-5 | 结果图片落盘 | done | 2026-03-04 | `OUTPUT_DIR` 逻辑 |
| M0-6 | 远程后台部署脚本 | done | 2026-03-04 | `scripts/remote_bootstrap.py` |

### M1（Whisper small）

| ID | 任务 | 状态 | 完成日期 | 备注 |
|---|---|---|---|---|
| M1-1 | 增加音频上传接口（transcribe） | done | 2026-03-04 | `app/main.py` 新增 `POST /asr/whisper/transcribe`；`requirements.txt` 增加 `python-multipart`；`python -m compileall app/main.py` 通过 |
| M1-2 | 集成 whisper small 推理 | done | 2026-03-04 | `app/main.py` 新增 `WhisperTranscriber` 并接入 `/asr/whisper/transcribe`；`MOCK_ASR=1` 下 FastAPI `TestClient` 调用返回 `status=200` 与转写文本 |
| M1-3 | 返回结构化结果（全文+分段+时间戳） | done | 2026-03-04 | `app/main.py` 新增 `segments` schema（`start_sec/end_sec/text`）并解析 whisper `chunks.timestamp`；`MOCK_ASR=1` 下 `TestClient` 响应包含 `segments` |
| M1-4 | 前端增加 ASR 页签 | done | 2026-03-04 | `app/static/index.html` 新增 `ASR Whisper` 页签、音频上传控件和转写结果展示；前端请求接入 `/asr/whisper/transcribe` |
| M1-5 | 增加 ASR curl 示例与文档 | done | 2026-03-04 | `README.md` 新增 ASR curl 示例、返回字段示例（含 `segments`）和 ASR 相关环境变量说明 |

### M2（OmniParser）

| ID | 任务 | 状态 | 完成日期 | 备注 |
|---|---|---|---|---|
| M2-1 | 引入 OmniParser 依赖和权重初始化流程 | done | 2026-03-04 | `scripts/install_runtime.sh` 新增可开关 OmniParser 安装/权重下载流程（repo+requirements+HF 权重）；`app/main.py` 的 `healthz.omniparser` 输出 `enabled/ready/missing_files`；`colab-run.yaml` 与 `README.md` 已补充配置与用法 |
| M2-2 | 新增 `/ui/parse` 接口 | done | 2026-03-04 | `app/main.py` 新增 `POST /ui/parse`（鉴权+图片上传+解析响应）；`TestClient` 调用返回 `status=200` |
| M2-3 | 定义统一 JSON schema（元素、bbox、置信度） | done | 2026-03-04 | 新增 `UiElement` schema：`element` + `bbox[4]` + `confidence` + `text`；`UiParseResponse.elements` 按统一结构返回 |
| M2-4 | 前端增加 UI 解析页签 | done | 2026-03-04 | `app/static/index.html` 新增 `UI Parse` 页签、截图上传与结果展示；前端请求接入 `/ui/parse` |
| M2-5 | 增加许可合规检查清单 | done | 2026-03-04 | 新增 `docs/05-omniparser-license-checklist.md`，覆盖个人非商用声明、商用前必检项、变更触发器与合规记录模板；`README.md` 已添加入口 |

### M3（统一调度）

| ID | 任务 | 状态 | 完成日期 | 备注 |
|---|---|---|---|---|
| M3-1 | 统一 Job 模型（task_type） | done | 2026-03-04 | `app/main.py` 新增 `TaskType` 并将 image/asr/ui_parse 全部纳入统一 `JobResponse`（含 `task_type`）；`/jobs/{job_id}` 可查询三类任务；`TestClient` 已验证三类任务写入与读取 |
| M3-2 | heavy/light 双队列与并发阈值 | todo | - | |
| M3-3 | GPU 重任务并发保护（=1） | todo | - | |
| M3-4 | 错误码与重试语义标准化 | todo | - | |

### M4（运维与观测）

| ID | 任务 | 状态 | 完成日期 | 备注 |
|---|---|---|---|---|
| M4-1 | 标准化部署命令与回收命令 | done | 2026-03-04 | 新增统一入口 `scripts/ops.sh`（start/status/stop/restart/recycle）；`colab-run.yaml` 与 `scripts/remote_bootstrap.py` 切到 `ops.sh start`；`bash scripts/ops.sh --help` 与 `... recycle --dry-run` 已验证 |
| M4-2 | 指标输出（队列长度、任务耗时） | done | 2026-03-04 | `app/main.py` 在 `healthz` 增加 `metrics.queue/image_jobs/asr_jobs`，含队列长度与任务耗时（last/avg）；`MOCK_IMAGEGEN=1 MOCK_ASR=1` 下 `TestClient` 验证计数与耗时字段 |
| M4-3 | 显存监控与熔断阈值 | done | 2026-03-04 | `app/main.py` 新增 `metrics.gpu_memory` 与 `guard`（阈值/触发次数/原因）；新增 `GPU_MEMORY_BREAKER_THRESHOLD_RATIO` 与 `GPU_MEMORY_FORCE_OPEN`；`TestClient` 验证正常路径与 `429` 熔断路径 |
| M4-4 | 失败任务诊断模板 | done | 2026-03-04 | 新增 `docs/04-failure-diagnosis-template.md`，覆盖鉴权/队列/显存熔断/推理/tunnel 分类排查、采集命令与恢复记录模板；`README.md` 已补充入口链接 |

## 3. 当前阻塞项

| ID | 阻塞描述 | 影响范围 | 下一步 |
|---|---|---|---|
| B-001 | OmniParser 商用许可策略待确认（个人非商用不阻塞） | M2 | 当前按个人使用继续推进 M2；若转商用再补充合规评审 |

## 4. 下一个执行周期（Next Sprint）

1. 目标：推进 M3-2（heavy/light 双队列与并发阈值）。
2. 验收标准：
- 按任务类型进入 heavy/light 队列；
- 可配置每类并发阈值；
- 队列满时返回明确错误；
- `healthz` 可观察两类队列指标。
