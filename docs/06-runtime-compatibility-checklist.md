# Runtime 兼容性检查清单（M6-3）

最后更新：2026-03-05

## 1. 目标

在启用 OmniParser 依赖 pin（`paddleocr<3`、`transformers==4.53.3`、`langchain<0.2`）后，快速判断是否对现有能力造成回归：

1. image_gen
2. asr_whisper_small
3. ui_parse_omniparser

## 2. 执行时机

1. 每次修改 `scripts/install_runtime.sh` 后。
2. 每次升级 Colab runtime / 关键 Python 包后。
3. 每次更换 OmniParser repo ref 或模型版本后。

## 3. 检查步骤

### Step A：安装与依赖一致性

```bash
export OMNIPARSER_ENABLED=1
bash scripts/install_runtime.sh
```

安装脚本会在 OmniParser 分支输出 `pip check (non-fatal)`，用于暴露依赖冲突。

如需更保守模式（允许 langchain 拉全量依赖）：

```bash
export OMNIPARSER_LANGCHAIN_INSTALL_MODE=full
```

默认是副作用更小的模式：

```bash
export OMNIPARSER_LANGCHAIN_INSTALL_MODE=no-deps
```

### Step B：基础能力回归（推荐）

```bash
bash scripts/ops.sh verify-regression
```

通过标准：命令末尾输出 `verify_runtime_regression_passed=1`。

该回归覆盖：

1. `/generate` 提交 + `/jobs/{job_id}` 查询 + `/jobs/{job_id}/image` 下载
2. `/asr/whisper/transcribe`
3. `/ui/parse`（mock 与 native import smoke）

### Step C：OmniParser 真机验收（可选）

```bash
API_BEARER_TOKEN="$API_BEARER_TOKEN" bash scripts/ops.sh verify-uiparse --expect-engine-mode native
```

## 4. 失败处理

1. `verify-regression` 失败：先看失败模块（image/asr/ui_parse）和返回体，再查看 `/tmp/colab-imagegen/api.log`。
2. `pip check` 冲突过多：优先保留核心运行链路，必要时临时关闭 `OMNIPARSER_RUN_PIP_CHECK`，但需在进度文档注明风险。
3. 若怀疑 `langchain` 相关副作用：确认当前 `OMNIPARSER_LANGCHAIN_INSTALL_MODE` 是否为 `no-deps`。

## 5. 记录模板（建议复制到进度文档）

1. Runtime 环境：endpoint / Python 版本 / commit
2. 安装命令与关键 env
3. `pip check` 摘要（冲突数量和关键包）
4. `verify-regression` 结果
5. 是否需要回滚或继续观察
