# colab-imagegen

在 Colab T4 runtime 上运行的图片生成服务骨架，支持通过 `cloudflared` 暴露 API。

## 落地计划

1. 先用 `colab-cli` 分配 T4，并在 runtime 内按固定 commit 拉取代码。
2. 执行 `scripts/colab_bootstrap.py --config colab-run.yaml`，自动完成安装与启动。
3. 服务层保持单进程单 worker，所有请求进入内存队列，避免 T4 显存争抢。
4. 隧道层默认 Quick Tunnel 验证，生产改成 `CF_TUNNEL_TOKEN` 的 managed tunnel。
5. 运维层通过 `service_status.sh`/`stop_service.sh` 检查和停止进程，最后 `assign rm` 回收资源。

## 项目结构

- `app/main.py`: FastAPI 服务、Bearer 鉴权、CORS、任务队列。
- `app/static/index.html`: 简单前端页面（`/`）。
- `scripts/install_runtime.sh`: 安装 Python 依赖与 cloudflared。
- `scripts/start_service.sh`: 启动 API + cloudflared。
- `scripts/service_status.sh`: 查看运行状态与公网地址。
- `scripts/stop_service.sh`: 停止服务。
- `scripts/colab_bootstrap.py`: 读取 `colab-run.yaml` 的配置驱动启动器。
- `colab-run.yaml`: Colab 启动配置模板。

## 预置接口

- `GET /healthz`: 健康检查。
- `GET /`: 简单前端页面。
- `POST /generate`: 提交图片任务，返回 `job_id`（需要 Bearer Token）。
- `GET /jobs/{job_id}`: 查询任务状态（需要 Bearer Token）。
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
bash scripts/service_status.sh
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

本地快速验证（不下载模型）可使用：

```bash
export API_BEARER_TOKEN='dev-local-token'
MOCK_IMAGEGEN=1 python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 5) 停止与回收

在 runtime shell 里：

```bash
cd /content/colab-imagegen
bash scripts/stop_service.sh
```

在本地：

```bash
npx --yes --package="$PKG" colab-cli -- assign rm <endpoint>
```

## 关键环境变量

- `MODEL_ID` 默认 `runwayml/stable-diffusion-v1-5`
- `API_BEARER_TOKEN` 必填，所有生成相关 API 都需要 `Authorization: Bearer <token>`
- `OUTPUT_DIR` 默认 `/tmp/colab-imagegen/outputs`，Colab 建议设为 `/content/.cache/colab-imagegen/outputs`
- `CORS_ALLOW_ORIGINS` 默认 `*`，可配置为逗号分隔白名单（例如 `http://localhost:3000,https://your-ui.example.com`）
- `CORS_ALLOW_CREDENTIALS` 默认 `false`
- `PORT` 默认 `8000`
- `MAX_QUEUE_SIZE` 默认 `16`
- `MAX_STEPS` 默认 `30`
- `MAX_WIDTH` 默认 `768`
- `MAX_HEIGHT` 默认 `768`
- `CF_TUNNEL_TOKEN` 为空时使用 Quick Tunnel

## 说明

- 图片会落盘到 `OUTPUT_DIR`，默认不进 Git；Colab runtime 回收后文件会随实例清空。
- 当前是内存态任务元数据队列，runtime 重启后任务记录会丢失。
- 该骨架面向 Colab T4 单机推理，不包含多实例扩缩容。
