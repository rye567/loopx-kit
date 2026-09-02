# LoopX 30 分钟快速通道执行手册

适用场景：**小-中范围 STANDARD 级变更**，目标在不减少任何质量门的前提下把完整流程压进 30 分钟。
本手册的每条加速手段都对应 `loopx/workflow.md` 的既有条款，不绕过任何规则。

## 一、耗时结构：先知道时间花在哪

完整流程 17 个阶段的耗时按性质分三类，优化空间完全不同：

| 类别 | 内容 | 量级 | 结论 |
|---|---|---|---|
| 机械操作 | controller 命令、schema 校验、健康检查核心项 | 毫秒-秒级 | 不占时间，无需优化 |
| LLM 生成 | interview/spec/solution/test_plan/review/审计/报告 文档撰写 | 每阶段 1-5 分钟，串行累加是最大头 | 主战场 |
| 人工等待 | 两处确认门：需求采访、方案审核（`NEED_HUMAN` → `confirm-stage`，见 workflow.md「人工确认」） | 不可控，用户离开多久停多久 | 用契约预留的出口 |

瓶颈在流程编排，不在工具。30 分钟的目标是压缩后两类。

## 二、五个加速手段（全部契约内合法）

### 1. 并行化 LLM 生成（最大杠杆）

依据：workflow.md「调用契约」明确授权按阶段委派子 agent。

- **审核阶段并行**：spec_review / solution_review / test_review / quality_audit / code_review 各自独立视角，用子 agent 同时执行再汇总结论。5 个审核环节从串行约 8 分钟压到约 2 分钟。
- **开发阶段多 worker**：worklist 每个 item 已有 `owner_agent` / `read_scope` / `write_scope` / `dependencies` 字段（workflow.md「Worklist 状态」），互不冲突的写入范围声明后直接并行；约束是同一文件、同一契约、同一测试类不得多 worker 同改（workflow.md「本地执行硬约束」）。
- **重叠等待窗口**：方案审核发用户确认的同时，让子 agent 起草测试用例——确认回来时测试设计已就绪，只补审核。把「人等机器」变成「机器等人」。

### 2. 一次性结构化产物，不走多轮转换

依据：workflow.md「Controller 和 Schema」—— `record-stage --artifact-file` 直接导入结构化 JSON。

solution、test_plan、quality_result 等结构化产物直接生成 JSON 再导入，跳过「markdown → JSON → 校验」的三轮往返，每阶段省 1-2 分钟返工。

### 3. 两处确认门改为「快速确认」

不取消确认，降低用户响应成本：

- 采访问题一次给全，每题附推荐答案（“如无异议按推荐执行”），用户一分钟勾完，而不是来回四五轮。
- workflow.md 默认行为第 10 条：显式 `auto_until_blocked` 只豁免需求采访和方案审核两处确认门，不跳过任何阶段，遇到 `BLOCKED` 或高风险动作仍停止。这是契约内的声明式降级，不是违规跳过。
- 高风险动作（git push、清库、生产写入等）即使授权全自动也仍需单独确认（workflow.md 默认行为第 9 条）。

### 4. 让风险分级诚实工作——LIGHT 是合法快路

依据：workflow.md「阶段状态机」——显式 LIGHT 允许跳过 7 个审核阶段（`spec_review`、`solution_design`、`solution_review`、`test_design`、`test_review`、`quality_audit`、`release_readiness`），唯一事实源是 `loopx/tools/loopx_controller_contracts.py` 的 `MODE_SKIPPABLE_STAGES`。

改文档、改文案类需求硬走 FULL 是流程错配。把 `risk.yml` 触发条件校准好，小变更自动落 LIGHT；但非显式 LIGHT 不得走轻流程，执行中发现实际影响超限必须立即升级（workflow.md「写入保护」）。

### 5. 健康检查用项目自身快路径

核心检查毫秒级，耗时在编译/测试命令。`loopx-policy.yml` 只配定向测试而非全量回归，全量留给 CI——符合 workflow.md 默认行为第 11 条「本地 PASS 只代表本地验证，未接入 CI 时最终报告必须写明 CI/远端未覆盖」。

## 三、30 分钟预算表（STANDARD 级，小范围变更）

| 阶段 | 常规串行 | 加速后 | 手段 |
|---|---|---|---|
| init + 环境检查 | 1 min | 0.5 min | controller 自动完成 |
| 需求采访 | 5-10 min | 3 min | 批量提问 + 推荐答案 |
| Spec 草稿 + 审核 | 6 min | 2.5 min | 一次成稿 + 子 agent 审核 |
| 模式选择 | 1 min | 0.5 min | 风险标签自动计算 |
| 方案设计 | 5 min | 4 min | 一份完整文档不返工 |
| 方案审核 + 确认 | 5-15 min | 2 min | 子 agent 审 + 预授权 |
| 测试设计 + 审核 | 5 min | 2 min | 利用确认等待窗口重叠 |
| 开发 | 10-20 min | 10 min | 多 worker 并行 |
| 质量审计 + 代码审查 | 4 min | 1.5 min | 两个子 agent 并行 |
| 测试执行 | 3 min | 2 min | 定向测试 |
| health + 发布就绪 + 最终报告 | 2 min | 1.5 min | 机械操作 |
| **合计** | **45-60+ min** | **~30 min** | |

## 四、诚实限定

此预算对**小-中范围 STANDARD 变更**成立。真跨模块、十几个 work item 的大变更压缩不了开发本身——正确做法是拆成多个 run 分批交付，而不是硬塞 30 分钟。质量门的价值恰恰体现在大变更上，为它赶时间才是真偷工减料。

## 五、执行前检查清单

- [ ] 变更范围确实适合 STANDARD/LIGHT（风险标签已过 `risk.yml` 评估）
- [ ] 用户已明确预授权（如适用「本次全自动」）
- [ ] worklist 的 `write_scope` 互不冲突已声明（多 worker 时）
- [ ] 子 agent 的任务说明自包含（不含本会话上下文依赖）
- [ ] 高风险动作清单已列出，全自动授权不覆盖它们
