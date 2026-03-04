# Colab Multi-Model Service: 运行架构与目标

最后更新：2026-03-04

## 1. 背景与愿景

本项目目标是把 Colab T4 运行时升级为一个可远程访问的多模型能力服务，统一承载：

1. `image_gen`：文生图（当前已可用）。
2. `asr_whisper_small`：语音转写（计划中）。
3. `ui_parse_omniparser`：UI 元素识别（计划中）。

对外通过 `cloudflared tunnel` 暴露，控制面通过 `colab-cli` 进行实例分配、远程执行和回收。

## 2. 运行架构

### 2.1 控制面（Control Plane）

1. 本地终端 + `colab-cli`：创建/删除 assignment、触发远程 bootstrap。
2. 版本治理：通过 Git commit pin 保证可复现部署。
3. 远程启动：优先 `colab-cli exec --no-wait`，任务后台执行，避免本地阻塞。

### 2.2 数据面（Data Plane）

1. 服务框架：`FastAPI + Uvicorn (workers=1)`。
2. 对外入口：`cloudflared`（开发可 Quick Tunnel，长期推荐 Named Tunnel + Access）。
3. 安全：Bearer Token 鉴权（`API_BEARER_TOKEN`），并开启 CORS。

### 2.3 推理调度层（Orchestration）

1. 统一任务模型：不同能力共用提交/状态查询机制。
2. 建议队列分层：
- `heavy_queue`：ImageGen / OmniParser（GPU 重任务，默认并发 1）。
- `light_queue`：Whisper small（可 CPU 并发）。
3. 核心原则：优先稳定成功率，避免 T4 显存争抢导致 OOM。

### 2.4 存储层（Storage）

1. 输出目录：`OUTPUT_DIR`（Colab 建议 `/content/.cache/colab-imagegen/outputs`）。
2. 模型缓存：`HF_HOME`（建议 `/content/.cache/huggingface`）。
3. 注意：Colab 实例释放后，本地磁盘内容会失效（除非另做持久化到 Drive/GCS）。

## 3. 当前能力范围（As-Is）

已落地：

1. ImageGen API。
2. Bearer Token 鉴权。
3. 简易 Web UI（`/`）。
4. CORS。
5. 结果图片落盘。
6. 远程后台部署脚本（`remote_bootstrap.py`）。

未落地：

1. Whisper small 集成。
2. OmniParser 集成。
3. 多能力统一任务路由和队列治理。
4. 持久化任务数据库与历史检索。

## 4. 目标架构（To-Be）

### 4.1 API 目标

1. `POST /generate`（image_gen）
2. `POST /asr/whisper/transcribe`
3. `POST /ui/parse`
4. `GET /jobs/{job_id}`
5. `GET /jobs/{job_id}/artifact`（统一产物下载）

### 4.2 稳定性目标

1. GPU 重任务串行保护（并发=1）。
2. 超时、队列上限、拒绝策略（429）完整可用。
3. 运行日志可追踪：请求 ID、任务 ID、耗时、失败原因。

### 4.3 运维目标

1. 一键部署（本地命令触发，远程后台运行）。
2. 一键停机与资源回收（防止 CCU 空耗）。
3. 可观测：`healthz` + 关键指标（队列长度、VRAM 使用等）。

## 5. 非目标（当前阶段）

1. 多节点集群调度。
2. 高可用 SLA（Colab 本身不适合承诺生产级 SLA）。
3. 多租户复杂权限系统。

## 6. 风险与约束

1. Colab 运行时生命周期不稳定（可能被回收）。
2. Quick Tunnel 无 SLA，生产不建议长期使用。
3. T4 显存 16GB，重模型并行易冲突。
4. OmniParser 模型许可需单独合规评估（商用前必须确认）。
