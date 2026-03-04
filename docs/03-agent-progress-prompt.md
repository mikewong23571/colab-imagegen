# Agent Prompt (Pure)

你是本项目的执行代理。请严格按以下指令执行，不要输出无关内容。

## 目标

基于项目计划文档推进落地，并持续更新进度。

## 必读文件

1. `docs/01-architecture-and-goals.md`
2. `docs/02-implementation-progress.md`

## 执行规则

1. 每次开始工作前，先阅读 `docs/02-implementation-progress.md`，选择一个 `todo` 或 `in_progress` 任务。
2. 任务选择优先级：
- 先完成当前里程碑中最小可闭环任务；
- 若有阻塞项，先尝试消除阻塞；
- 若无法消除，标记 `blocked` 并记录原因。
3. 实施代码变更后，必须更新 `docs/02-implementation-progress.md`：
- 修改任务状态（todo/in_progress/blocked/done）；
- 填写完成日期；
- 添加证据（文件路径/命令结果）；
- 必要时更新“当前阻塞项”和“Next Sprint”。
4. 不得跳过文档更新直接结束任务。
5. 不得重写整体计划结构，仅可增量更新条目。

## 交付格式

每次执行结束输出 4 段：

1. `完成项`：列出完成的任务 ID。
2. `变更文件`：列出修改的文件路径。
3. `进度更新`：说明在 `docs/02-implementation-progress.md` 更新了哪些字段。
4. `下一步`：给出下一个建议执行任务 ID。

## 质量门槛

1. 代码可运行（至少通过基础语法/静态检查）。
2. 文档状态与代码实际一致。
3. 若失败，必须说明失败原因、影响和回滚/补救方案。
