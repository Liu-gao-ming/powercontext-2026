# Jev 1.13 Memory 写入门实测结果

2026-10-14，通过 OpenRouter 实际调用 `typesafe/jev-1.13`。公开 XNLI 题集包含 600 条 calibration、
1,800 条 held-out；固定配置为 `hold_on=yes;threshold=none`。

问题适配器版本为 `powercontext.decision.systemone.jev.v2:typesafe/jev-1.13`，策略版本为
`1;hold_on=yes;threshold=none`。实测记录的评测代码摘要为
`8edd5373032f3cca59c607445083ac6b85b9353e79b4ed621639852ecf4e1fe8`。
该摘要标识调用时的实验实现，不表示当前代码重新执行过云端测量。

**建议：本次模型、问题和配置继续保持 shadow/advisory。**
独立题集误拦了 107 条有支持证据的候选，其中中文 82/299（27.42%）；包含未裁决后的放行，
另有 176/1200 条本应暂缓的候选被放行。这是具体配置的实测建议，不是官方统一及格线。

## 独立评估

| 语言 | 题目数 | 已裁决 | 误拦／应接受（已裁决） | 误放／应暂缓（已裁决） | 含未裁决的误放／应暂缓 |
| --- | ---: | ---: | --- | --- | --- |
| 英文 | 900 | 891 | 25/296（8.45%） | 84/595（14.12%） | 89/600 |
| 中文 | 900 | 889 | 82/299（27.42%） | 77/590（13.05%） | 87/600 |
| 总体 | 1800 | 1780 | 107/595（17.98%） | 161/1185（13.59%） | 176/1200 |

输入恢复率 100%，实质判断覆盖率 98.89%。20 条未裁决分别为 10 条 abstain 和 10 条响应校验失败。
它们不进入已裁决质量分母；涉及应暂缓题时，进入包含 fail-open 的放行效果。
所有 `used_fallback=true` 记录保持 `coverage=unadjudicated`。
中英文是对应题，不把总体当作 1,800 个独立场景。

最后一次尝试 P50/P95 调用耗时为 0.706/1.380 秒。正式 calibration + 最终 held-out 的已知费用小计为
**USD 0.049940184**。13 条响应校验失败及早期连接失败缺少可靠 usage，完整费用和 token 总量未知。
最终用量不包含旧失败尝试的未知费用，重试可能重复计费。六题 smoke 和诊断不参与正式成绩。

样本 HOLD 为 1131/1800（62.83%），FLAG 为 0。HOLD 不写入，FLAG 允许写入，按实际效果评分。
这些比例不外推生产负担；人工处理时间和部署摩擦预算未测量。

## Calibration 比较与冻结

按已声明规则最小化误拦率与包含 fallback 的误放率的平均值；冻结配置后才调用 held-out。

| 配置 | 误拦率 | 误放率（含 fallback） |
| --- | ---: | ---: |
| `hold_on=yes;threshold=none` | 15.08% | 14.00% |
| `hold_on=yes;threshold=0.5` | 8.04% | 28.50% |
| `hold_on=yes;threshold=0.7` | 4.52% | 42.00% |
| `hold_on=yes;threshold=0.9` | 1.51% | 77.50% |
| `hold_on=no;threshold=none` | 84.92% | 86.75% |
| `hold_on=no;threshold=0.5` | 65.83% | 94.00% |
| `hold_on=no;threshold=0.7` | 54.27% | 95.75% |
| `hold_on=no;threshold=0.9` | 31.66% | 98.50% |

## 初次故障与两轮补测

首轮仅 1074/1800 已裁决：711 条连接不可用、3 条超时、7 条响应校验失败、5 条 abstain。
首轮含失败后的误放为 597/1200（49.75%）；这不是已回答题的模型误判率。
新连接可用，故障原因未确定。先补测 714 条网络失败，再补测仍失败的 7 条。
没有替换已裁决回答、主动 abstain 或格式异常结果，没有改模型、问题或配置。
正式后端尝试为 600 + 1800 + 714 + 7 = 3121 次。

逐题最后一次耗时不能冒充全流程等待时间。

## 评估范围

本实验仅把公开句对发送到云端，随后本地复用 Runtime 规则，不验证正式 hosted-redacted 传输。
XNLI 不证明生产 Memory 分布、多条证据、业务严重错误、真实历史引用覆盖或训练数据无重叠。
私有授权和精确版本行为由 Server/SQLite 受控推理测试验证，不冒充生产部署验收。
本实验不会修改部署模式。问题或模型发生变化后，需要重新校准，并使用未参与修改决策的独立题集。

## 回滚步骤

如果部署之后启用了 `enforcing`，需要撤回时：

1. 将 `POWERCONTEXT_SERVER_RUNTIME_MEMORY_WRITE_GATE_MODE=shadow`，重启受影响的 Runtime 并核对生效配置。
2. 执行一次有界写入探针，确认原本会被 gate 暂缓的写入正常完成，且 Scope 私有观察记录仍保存评估。
3. 如需同时停止模型调用，设置 `POWERCONTEXT_SERVER_RUNTIME_MEMORY_WRITE_GATE_PRIVACY_BOUNDARY=no_external_call`。
   如需完全关闭 gate，设置 `POWERCONTEXT_SERVER_RUNTIME_MEMORY_WRITE_GATE_ENABLED=false`；这也会停止观察记录。
4. 比较变更前后的暂缓数量和 fallback 原因，不重写历史 Memory revision、citation 或观察记录。
   enforcing 期间已写入内容如需修正，使用正常的 Memory 审核与生命周期操作。
