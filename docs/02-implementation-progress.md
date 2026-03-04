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
| M1 | Whisper small 集成与 API 发布 | todo |
| M2 | OmniParser 集成与 UI 解析 API 发布 | todo |
| M3 | 多能力统一调度与资源治理 | todo |
| M4 | 运维自动化与观测增强 | todo |

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
| M1-1 | 增加音频上传接口（transcribe） | todo | - | |
| M1-2 | 集成 whisper small 推理 | todo | - | |
| M1-3 | 返回结构化结果（全文+分段+时间戳） | todo | - | |
| M1-4 | 前端增加 ASR 页签 | todo | - | |
| M1-5 | 增加 ASR curl 示例与文档 | todo | - | |

### M2（OmniParser）

| ID | 任务 | 状态 | 完成日期 | 备注 |
|---|---|---|---|---|
| M2-1 | 引入 OmniParser 依赖和权重初始化流程 | todo | - | |
| M2-2 | 新增 `/ui/parse` 接口 | todo | - | |
| M2-3 | 定义统一 JSON schema（元素、bbox、置信度） | todo | - | |
| M2-4 | 前端增加 UI 解析页签 | todo | - | |
| M2-5 | 增加许可合规检查清单 | todo | - | |

### M3（统一调度）

| ID | 任务 | 状态 | 完成日期 | 备注 |
|---|---|---|---|---|
| M3-1 | 统一 Job 模型（task_type） | todo | - | |
| M3-2 | heavy/light 双队列与并发阈值 | todo | - | |
| M3-3 | GPU 重任务并发保护（=1） | todo | - | |
| M3-4 | 错误码与重试语义标准化 | todo | - | |

### M4（运维与观测）

| ID | 任务 | 状态 | 完成日期 | 备注 |
|---|---|---|---|---|
| M4-1 | 标准化部署命令与回收命令 | in_progress | - | 已有脚本，可继续整理 |
| M4-2 | 指标输出（队列长度、任务耗时） | todo | - | |
| M4-3 | 显存监控与熔断阈值 | todo | - | |
| M4-4 | 失败任务诊断模板 | todo | - | |

## 3. 当前阻塞项

| ID | 阻塞描述 | 影响范围 | 下一步 |
|---|---|---|---|
| B-001 | OmniParser 最终依赖与许可策略未定 | M2 | 先完成技术 PoC，再做合规评审 |

## 4. 下一个执行周期（Next Sprint）

1. 目标：完成 M1（Whisper small）端到端。
2. 验收标准：
- 能上传音频并返回文本；
- 提供 curl 示例；
- 前端可提交音频并显示结果；
- 与现有鉴权与 CORS 兼容。
