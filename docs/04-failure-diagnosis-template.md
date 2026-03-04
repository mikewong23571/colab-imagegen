# Colab Multi-Model Service: 失败任务诊断模板

最后更新：2026-03-04

## 1. 基础信息

- 事件时间：
- 运行环境：`local` / `colab`
- 提交人：
- 任务类型：`image_gen` / `asr_whisper_small` / `ui_parse_omniparser`
- 请求 ID / Job ID / Upload ID：
- 是否可稳定复现：`yes` / `no`

## 2. 现场采集（先执行）

```bash
# 1) 服务状态与入口信息
bash scripts/ops.sh status

# 2) 健康与指标
curl -s "http://127.0.0.1:${PORT:-8000}/healthz" | jq '.'

# 3) 近期 API 日志
tail -n 200 /tmp/colab-imagegen/api.log

# 4) 近期 tunnel 日志
tail -n 200 /tmp/colab-imagegen/cloudflared.log

# 5) 运行态文件（若存在）
cat /tmp/colab-imagegen/service.env
```

## 3. 故障分类（勾选）

- [ ] `401` 鉴权失败 (`auth_error`)：`Missing bearer token` / `Invalid bearer token`
- [ ] `429` 队列满 (`queue_full`)：`Queue is full`，系统建议根据 `retry_strategy` 自动重试
- [ ] `429` 显存熔断 (`circuit_breaker`)：`gpu memory guard is open (...)`，重试建议 `backoff_ms=5000`
- [ ] `400/404/413/422` 请求异常 (`invalid_request`)：尺寸、步数、文件类型、空文件或参数校验失败
- [ ] `500` 推理失败 (`internal_error`)：模型加载/执行异常
- [ ] tunnel 异常：公网地址不可达、cloudflared 退出
- [ ] 其他（补充）：

## 4. 分类排查路径

### A. 鉴权失败（401）

1. 核对请求头是否包含：`Authorization: Bearer <token>`。
2. 核对 runtime 环境变量：`API_BEARER_TOKEN` 与调用方 token 一致。
3. 若 token 轮换过，重启服务：

```bash
bash scripts/ops.sh restart
```

### B. 队列满（429 Queue is full）

1. 查看 `healthz.metrics.queue`：`size/capacity`。
2. 临时降低请求并发，等待队列回落。
3. 若确需更高吞吐，评估调大 `MAX_QUEUE_SIZE`（注意显存压力）。

### C. 显存熔断（429 gpu memory guard）

1. 查看 `healthz.metrics.gpu_memory.used_ratio` 与 `guard.threshold_ratio`。
2. 等待当前重任务完成后重试。
3. 临时策略（谨慎）：
   - 降低图像尺寸/步数；
   - 提高 `GPU_MEMORY_BREAKER_THRESHOLD_RATIO`（风险自担）；
   - 使用 `GPU_MEMORY_FORCE_OPEN=1` 仅用于演练，不要在生产开启。

### D. 推理失败（500）

1. 检查 `api.log` 具体异常栈。
2. 核对模型依赖是否完整安装：

```bash
bash scripts/install_runtime.sh
```

3. 本地快速隔离：`MOCK_IMAGEGEN=1` / `MOCK_ASR=1` 先验证服务链路。

### E. Tunnel 异常

1. `bash scripts/ops.sh status` 检查 `cloudflared` 进程。
2. 查看 `/tmp/colab-imagegen/cloudflared.log` 是否报错退出。
3. 重新启动：

```bash
bash scripts/ops.sh restart
```

## 5. 处置与恢复记录

- 临时处置动作：
- 恢复时间：
- 是否完全恢复：`yes` / `no`
- 残余风险：

## 6. 根因与补救

- 根因描述：
- 影响范围：
- 长期修复项：
- 回滚方案（如有）：
- 负责人：
- 预计完成时间：

