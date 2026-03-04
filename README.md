# colab-imagegen

在 Colab T4 runtime 上运行的图片生成服务骨架，支持通过 `cloudflared` 暴露 API。

## 项目文档入口

- `docs/01-architecture-and-goals.md`: 运行架构与目标。
- `docs/02-implementation-progress.md`: 计划落地进度跟踪（唯一事实源）。
- `docs/03-agent-progress-prompt.md`: 纯净 agent prompt（要求先读计划并更新进度）。
- `docs/04-failure-diagnosis-template.md`: 故障诊断模板（分类、命令、恢复记录）。
- `docs/05-omniparser-license-checklist.md`: OmniParser 许可合规检查清单（个人非商用/商用前复核）。

## 落地计划

1. 先用 `colab-cli` 分配 T4，并在 runtime 内按固定 commit 拉取代码。
2. 执行 `scripts/colab_bootstrap.py --config colab-run.yaml`，自动完成安装与启动。
3. 服务层保持单进程单 worker，所有请求进入内存队列，避免 T4 显存争抢。
4. 隧道层默认 Quick Tunnel 验证，生产改成 `CF_TUNNEL_TOKEN` 的 managed tunnel。
5. 运维层统一使用 `scripts/ops.sh`（start/status/stop/restart/recycle）管理进程与回收，避免命令漂移。

## 项目结构

- `app/main.py`: FastAPI 服务、Bearer 鉴权、CORS、任务队列。
- `app/static/index.html`: 简单前端页面（`/`）。
- `scripts/ops.sh`: 统一运维入口（start/status/stop/restart/recycle）。
- `scripts/install_runtime.sh`: 安装 Python 依赖与 cloudflared。
- `scripts/start_service.sh`: 启动 API + cloudflared（底层实现，通常由 `ops.sh start` 调用）。
- `scripts/service_status.sh`: 查看运行状态与公网地址（底层实现，通常由 `ops.sh status` 调用）。
- `scripts/stop_service.sh`: 停止服务（底层实现，通常由 `ops.sh stop` 调用）。
- `scripts/colab_bootstrap.py`: 读取 `colab-run.yaml` 的配置驱动启动器。
- `colab-run.yaml`: Colab 启动配置模板。

## 预置接口

- `GET /healthz`: 健康检查。
- `GET /`: 简单前端页面。
- `POST /generate`: 提交图片任务，返回 `job_id`（需要 Bearer Token）。
- `POST /asr/whisper/transcribe`: 上传音频并返回转写（全文 + 分段 + 时间戳，需要 Bearer Token）。
- `POST /ui/parse`: 上传 UI 截图并返回结构化元素（`element + bbox + confidence`，需要 Bearer Token）。
- `GET /jobs/{job_id}`: 查询统一任务状态（支持 image/asr/ui_parse，含 `task_type`，需要 Bearer Token）。
- `GET /jobs/{job_id}/image`: 下载 PNG（需要 Bearer Token）。

## colab-cli 操作流程

### 1) 固定 CLI 版本

`main` 在 2026-03-04 的 commit 是 `49963d9690f81087014e35c67eca5c7ad2463798`，建议固定这个 SHA 或你自己的验证 SHA。

```bash
export PKG='git+https://github.com/mikewong23571/colab-vscode.git#49963d9690f81087014e35c67eca5c7ad2463798'
```

### 2) 登录并申请 T4

```bash
npx --yes --package="$PKG" colab-cli -- login
npx --yes --package="$PKG" colab-cli -- quota
npx --yes --package="$PKG" colab-cli -- assign add --variant GPU --accelerator T4
npx --yes --package="$PKG" colab-cli -- assign list
```

记录 endpoint（例如 `m-s-abc123`）。

### 3) 进入 runtime 并启动

```bash
npx --yes --package="$PKG" colab-cli -- terminal --assign <endpoint>
```

在 runtime shell 里执行：

```bash
cd /content
if [ ! -d colab-imagegen/.git ]; then
  git clone <your-repo-url> colab-imagegen
fi
cd colab-imagegen
git fetch --all --tags
git checkout <your-commit-sha>

# 按需修改 colab-run.yaml 中 repo.url/ref，或者直接使用当前目录执行脚本
python scripts/colab_bootstrap.py --config colab-run.yaml
bash scripts/ops.sh status
```

如果你使用 managed tunnel，在启动前设置：

```bash
export CF_TUNNEL_TOKEN='<your-cloudflare-tunnel-token>'
```

### 4) 调用 API

```bash
curl -X POST "http://127.0.0.1:8000/generate" \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "a cinematic mountain landscape, golden hour",
    "width": 512,
    "height": 512,
    "num_inference_steps": 20,
    "guidance_scale": 7.0
  }'
```

ASR Whisper 示例：

```bash
curl -X POST "http://127.0.0.1:8000/asr/whisper/transcribe" \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -F "file=@./sample.wav"
```

返回示例（字段会随模型输出略有变化）：

```json
{
  "job_id": "0ce7eb8f-08ef-4172-8359-0ce4f0e1e76f",
  "upload_id": "0ce7eb8f-08ef-4172-8359-0ce4f0e1e76f",
  "status": "succeeded",
  "filename": "sample.wav",
  "content_type": "audio/wav",
  "size_bytes": 165432,
  "text": "hello world ...",
  "segments": [
    { "start_sec": 0.0, "end_sec": 1.9, "text": "hello" },
    { "start_sec": 1.9, "end_sec": 3.1, "text": "world" }
  ],
  "language": "en",
  "model_id": "openai/whisper-small",
  "elapsed_ms": 742
}
```

UI 解析示例：

```bash
curl -X POST "http://127.0.0.1:8000/ui/parse" \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -F "file=@./screen.png"
```

返回示例（`engine_mode` 可能为 `mock` 或 `native`）：

```json
{
  "job_id": "cc442a6e-34f9-4c58-92be-9f5f726f4127",
  "parse_id": "cc442a6e-34f9-4c58-92be-9f5f726f4127",
  "status": "succeeded",
  "filename": "screen.png",
  "content_type": "image/png",
  "size_bytes": 120304,
  "model_id": "microsoft/OmniParser-v2.0",
  "engine_mode": "native",
  "elements": [
    { "element": "text", "bbox": [48.1, 98.0, 340.2, 141.4], "confidence": 0.5, "text": "Settings" },
    { "element": "icon_interactive", "bbox": [40.0, 280.0, 190.0, 430.0], "confidence": 0.5, "text": "Search icon" }
  ],
  "elapsed_ms": 863
}
```

错误响应结构示例（例如 429 队列满）：

```json
{
  "error": "queue_full",
  "message": "heavy queue is full",
  "retry_strategy": {
    "should_retry": true,
    "backoff_ms": 2000,
    "max_retries": 3
  }
}
```

本地快速验证（不下载模型）可使用：

```bash
export API_BEARER_TOKEN='dev-local-token'
MOCK_IMAGEGEN=1 MOCK_ASR=1 python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

OmniParser 依赖与权重初始化（个人非商用场景）：

```bash
export OMNIPARSER_ENABLED=1
export OMNIPARSER_DOWNLOAD_WEIGHTS=1
bash scripts/install_runtime.sh
```

启用真实 OmniParser 推理（非 mock）：

```bash
export MOCK_UIPARSE=0
```

初始化结果可通过 `GET /healthz` 的 `omniparser` 字段查看（`enabled/ready/engine_mode/missing_files/load_error`）。

当 `MOCK_UIPARSE=0` 且 OmniParser 依赖/权重未就绪时，`POST /ui/parse` 将直接返回 `503 service_unavailable`（不进入 heavy 队列），用于快速暴露环境问题。

建议在 Colab runtime 用以下脚本做一次验收并留存证据：

```bash
API_BEARER_TOKEN="$API_BEARER_TOKEN" python scripts/verify_uiparse_native.py \
  --base-url "http://127.0.0.1:${PORT:-8000}" \
  --expect-engine-mode native
```

或使用统一入口：

```bash
bash scripts/ops.sh verify-uiparse --expect-engine-mode native
```

该脚本会输出：
- `health.omniparser.*`（enabled/ready/engine_mode/reason）
- `ui_parse.engine_mode/elements_count/elapsed_ms/parse_id`
- `health.metrics.ui_parse_jobs`（提交/成功/失败/耗时/最近元素数）

### 5) 停止与回收

在 runtime shell 里停止服务：

```bash
cd /content/colab-imagegen
bash scripts/ops.sh stop
```

在本地回收 assignment（推荐统一命令）：

```bash
cd /path/to/colab-imagegen
COLAB_CLI_PKG="$PKG" bash scripts/ops.sh recycle --endpoint <endpoint>
```

等价手工命令：

```bash
npx --yes --package="$PKG" colab-cli -- assign rm <endpoint>
```

### 6) 运行指标（healthz）

`GET /healthz` 现在包含 `metrics` 字段，可用于脚本采集：

- `metrics.queue.heavy.size/capacity/concurrency`
- `metrics.queue.heavy.runtime_limit/running/max_running_seen`
- `metrics.queue.light.size/capacity/concurrency`
- `metrics.image_jobs.submitted_total/succeeded_total/failed_total/last_duration_ms/avg_duration_ms`
- `metrics.asr_jobs.submitted_total/succeeded_total/failed_total/last_duration_ms/avg_duration_ms`
- `metrics.ui_parse_jobs.submitted_total/succeeded_total/failed_total/last_duration_ms/avg_duration_ms/last_elements_count/last_engine_mode`
- `metrics.gpu_memory.*`（含 `used_ratio`）与 `metrics.gpu_memory.guard.*`（熔断状态）

示例：

```bash
curl -s "http://127.0.0.1:8000/healthz" | jq '.metrics'
```

## 关键环境变量

- `MODEL_ID` 默认 `runwayml/stable-diffusion-v1-5`
- `WHISPER_MODEL_ID` 默认 `openai/whisper-small`
- `API_BEARER_TOKEN` 必填，所有生成相关 API 都需要 `Authorization: Bearer <token>`
- `OUTPUT_DIR` 默认 `/tmp/colab-imagegen/outputs`，Colab 建议设为 `/content/.cache/colab-imagegen/outputs`
- `ASR_MAX_UPLOAD_BYTES` 默认 `26214400`（25MB）
- `CORS_ALLOW_ORIGINS` 默认 `*`，可配置为逗号分隔白名单（例如 `http://localhost:3000,https://your-ui.example.com`）
- `CORS_ALLOW_CREDENTIALS` 默认 `false`
- `PORT` 默认 `8000`
- `MAX_QUEUE_SIZE` 默认 `16`（兼容旧配置，作为 `HEAVY_QUEUE_MAX_SIZE` 的默认值）
- `HEAVY_QUEUE_MAX_SIZE` 默认继承 `MAX_QUEUE_SIZE`（heavy: image_gen + ui_parse）
- `LIGHT_QUEUE_MAX_SIZE` 默认 `16`（light: asr_whisper_small）
- `HEAVY_QUEUE_CONCURRENCY` 默认 `1`（worker 数；实际 heavy 推理并发由运行时硬限制为 1）
- `LIGHT_QUEUE_CONCURRENCY` 默认 `2`
- `MAX_STEPS` 默认 `30`
- `MAX_WIDTH` 默认 `768`
- `MAX_HEIGHT` 默认 `768`
- `MOCK_ASR=1` 可启用 ASR mock 返回（用于本地联调）
- `GPU_MEMORY_BREAKER_THRESHOLD_RATIO` 默认 `0.92`，当当前 `used_ratio` 超阈值时拒绝新的重任务（image/ui_parse）
- `GPU_MEMORY_FORCE_OPEN=1` 可强制打开熔断（用于演练/测试）
- `OMNIPARSER_ENABLED` 默认 `0`，开启后在安装阶段拉取 OmniParser 依赖并可初始化权重
- `OMNIPARSER_REPO_URL` 默认 `https://github.com/microsoft/OmniParser.git`
- `OMNIPARSER_REPO_REF` 默认 `master`
- `OMNIPARSER_DIR` 默认 `/content/.cache/omniparser/repo`
- `OMNIPARSER_WEIGHTS_DIR` 默认 `/content/.cache/omniparser/weights`
- `OMNIPARSER_DOWNLOAD_WEIGHTS` 默认 `1`
- `OMNIPARSER_CAPTION_MODEL_NAME` 默认 `florence2`
- `OMNIPARSER_BOX_THRESHOLD` 默认 `0.05`
- `OMNIPARSER_DEFAULT_CONFIDENCE` 默认 `0.5`（当 OmniParser 输出不含置信度时使用）
- `MOCK_UIPARSE` 默认 `1`，为 `0` 时调用 OmniParser 原生推理
- `UI_PARSE_MAX_UPLOAD_BYTES` 默认 `10485760`（10MB）
- `CF_TUNNEL_TOKEN` 为空时使用 Quick Tunnel

## 说明

- 图片会落盘到 `OUTPUT_DIR`，默认不进 Git；Colab runtime 回收后文件会随实例清空。
- 当前是内存态任务元数据队列，runtime 重启后任务记录会丢失。
- 该骨架面向 Colab T4 单机推理，不包含多实例扩缩容。
