---
title: 上下文隐私与复用
description: 配置模型最终请求检查，并在当前授权下复用准确的准备上下文。
---

# 上下文隐私与复用

PowerContext 可以在模型请求真正发出前检查输入，在完整原始响应交付前检查输出。
准备 Atomic Memory 时还可以取得短期复用元信息，在同一个任务边界内保留准确准备值，
避免重复检索和查询 Embedding。模型仍会收到这段上下文；复用不会自动消除其输入 Token，也不保证供应商缓存命中。

这些能力直接使用现有 Runtime、Scope、Artifact 和 inference 组件，无需单独部署网关或增加数据库表。
SQLite、SeekDB、OceanBase 都使用既有权威记录。第三方执行器可以调用同一套公开 HTTP 契约。

## 配置受支持的目标

隐私策略由可信部署配置，入口是 `InferenceConfig.protection`。默认 `disabled` 保持原行为。
`shadow`、`advisory` 只观察已经物化的请求，不检查响应、不阻止交付，不能宣传为防止泄露；
`enforcing` 在必需检查失败时阻断或暂缓。表示优化和复用选项不能关闭强制检查。

例如，为一个受控文本模型端点构造配置：

```python
from pydantic import SecretStr

from powercontext.builtin.inference.privacy import InferenceProtectionConfig, UpstreamProfile
from powercontext.builtin.runtime import InferenceConfig

inference = InferenceConfig(
    generation_model="openai:approved-model",
    generation_base_url="https://models.example/v1",
    generation_model_settings={"max_tokens": 1024},
    protection=InferenceProtectionConfig(
        mode="enforcing",
        forbidden_values=(SecretStr("synthetic-secret-for-a-local-test"),),
        targets={
            "coding": UpstreamProfile(
                endpoint="https://models.example/v1/chat/completions",
                model="approved-model",
                protocol="openai-chat",
                context_window_tokens=32768,
                max_output_tokens=1024,
                validated_utf8_token_bound=False,
            )
        },
    ),
)
```

端点、模型和限制仅为示例。这个示例会暂缓发送：部署方需要先验证模型 tokenizer 和协议模板满足保守的
UTF-8 上界及配置的协议预留，再设置 `validated_utf8_token_bound=True`。字符数估算不是模型窗口的硬保证。
生成模型必须声明窗口、输出限制，最终请求也必须携带有效的正整数输出上限；超预算会暂缓。

Server 可通过 `POWERCONTEXT_SERVER_INFERENCE_PROTECTION` 设置相同对象的 JSON。
真实禁止值放在受保护的部署配置中，不写入提交的示例。若响应中的模型名不同于请求别名，
需要在 `response_models` 中明确列出获准别名。

规则检查配置的准确值，以及可选的有限格式：`email`、`private_key`、`api_token`。
检查覆盖原始 JSON、解码文本、工具参数和数字词法表示。重复 JSON 键、不透明图片/音频/文件、
未知请求字段、不完整输出和不支持的目标无法取得完整覆盖。
这不是通用 PII 分类器、语义 DLP 或工具执行授权。

有界的 `openai-responses` profile 要求显式提供布尔值 `store: false`；省略、null 或启用存储都会暂缓。
Responses 默认会存储响应，参见提供方的[数据保留约定](https://developers.openai.com/api/docs/guides/conversation-state#data-retention-for-model-responses)。
内置 Pydantic AI Responses 模型可在对应的 model settings 中设置 `openai_store: false`。
未展开的 `item_reference` 输入（包括省略 `type` 或将其设为 null 的引用）也会暂缓，因为无法检查其历史内容或把它计入窗口预算。
请提供受支持的完整文本和工具输入，而不是提供方的历史条目引用。
Anthropic 文档仅支持内联纯文本来源；PDF/base64、URL、Files API 引用和含图片的文档会暂缓，且不会签发请求绑定。

内置 OpenAI/Anthropic、Embedding、Rerank 路径把检查接在最终 HTTP transport，
原生 SDK 的格式转换先完成。Enforcing 关闭 SDK 传输层自动重试；原有阶段的输出 schema 重试仍有界，
每次实际发送都要检查。必需隐私错误会穿透 advisory 决策降级，不会变成空提取结果或已消费的 Memory HOLD。
Memory 写入 gate 遇到这类拒绝时，flush 会在发布前失败，Source 窗口在重启后仍待处理；
修正策略或模型输出后可重试。
无法控制最终传输边界的自定义推理对象，在 enforcing 模式下明确拒绝注入。

## 准备一次，每次复用都验证

`POST /v1/context/prepare` 默认仍返回 `schema`、`status`、`content`、`content_bytes`；
既有 `explain` 选项仍控制可选诊断。增加 `include_reuse: true` 后返回：

```json
{
  "schema": "powercontext.prepared-context-with-reuse.v1",
  "context": {
    "schema": "powercontext.prepared-context.v1",
    "status": "ready",
    "content": "<完整的历史上下文>",
    "content_bytes": 26
  },
  "reuse": {
    "schema": "powercontext.context-reuse.v1",
    "constraints": {
      "preserve": ["content", "citations", "trust", "order"],
      "refresh_on": ["permission_change", "source_ineligible", "context_boundary"]
    },
    "expires_at": "2026-10-26T12:05:00Z",
    "binding": "<不透明的加密绑定>"
  }
}
```

正文、字节数、时间和绑定只是形状示例。实际使用返回的准确值。
Python Client 返回 `PreparedContext` 或 `PreparedContextWithReuse`，读取字段前先判断结果类型。
进程内沿用 `runtime.context.for_scope(scope_id).prepare(request)`；
`validate_reuse` 检查保留的证明，不做检索或 Embedding。嵌入式应用自行提供当前可信授权上下文。

首个复用 profile 支持单 Scope、实际选中的 Atomic Memory，不支持 context references 或代码。
它记录最终选中的当前 revision、生命周期版本和完整有界历史依赖。合并结果的旧祖先在领域规则允许时仍可作为证据。
根记忆被修订或遗忘、访问撤销、引用范围变化、绑定过期、Source 不再符合资格，都会停止旧值复用。
请求复用时实际选中了其他 family，会明确拒绝；普通 prepare 保持原支持范围。

绑定经过认证加密，期限五分钟，最多 64 个依赖、八层祖先。绑定不是访问权限。
Server 用既有签名秘密派生独立用途的密钥，并绑定持久 Server 身份；同部署的多个进程需要共享该秘密。
进程内 Runtime 默认使用临时密钥。没有新增 prompt 或响应缓存表。

## 检查真正的最终载荷

统一调用 `POST /v1/context/check`，扁平操作名 `check_context`，分两个时机：

1. `before_send`：原生协议渲染和缓存设置完成后，写出任何模型请求之前。
2. `before_release`：收齐完整原始响应后，交付正文、工具参数或错误内容之前。

两个阶段都沿用根级 `scope_id`、当前认证/委托和当前 Scope、资料权限。
`scope.read` 不授予模型付费或工具执行权限；请求字段不能降低部署策略。

版本化载体保存原始 JSON 文本，不接受重新拼装的摘要对象：

```json
{
  "scope_id": "<已有 Scope>",
  "phase": "before_send",
  "target_profile": "coding",
  "payload": {
    "schema": "powercontext.model-json-payload.v1",
    "headers": {"content-type": "application/json"},
    "body": "{\"model\":\"approved-model\",\"max_tokens\":1024,\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}"
  },
  "prepared_contexts": []
}
```

复用时，每个历史准备块都提交 binding 和 `powercontext.model-json-text-location.v1` 位置：
JSON pointer，以及解码后 UTF-8 文本的 `start_byte`、`end_byte` 半开区间。
必须定位准确的完整块，保留历史资料信任包装和顺序，并位于受支持的 user 消息文本中。
提升到 system/developer、修改正文、重叠或乱序位置、漏报准备块均会拒绝。
要求准备块的目标不能静默降级为不带绑定的纯检查。

只有 `allow` 且 `coverage.complete=true` 才能继续对应动作。
`block`、`withhold` 虽然返回 HTTP 200，但都禁止执行；只判断 HTTP 成功是不够的。
载体或位置不合法返回 422，权限失败返回 401/403，正文超限返回 413。
必需检查不可用时不发送、不交付；未配置 enforcing 的公开检查只返回暂缓。

发送批准绑定准确载荷、目标、策略、身份和 Scope。60 秒发送期限与五分钟响应关联期限分开：
把发送批准的 `binding` 作为 `before_release.request_binding`，提交完整响应载体及
`completion: "complete"`，再次验证当前策略、身份和准备依赖。
交付批准不能用于发送新请求，关联期限也不能延长发送期限；绑定不提供恰好一次执行保证。

公开 carrier 当前支持完整 JSON。本地 protected transport 另外支持单 choice 的 OpenAI Chat SSE，
需要显式设置 `allow_streaming=true`，其他流式 profile 暂缓发送。
交付前按字节、时间、并发上限缓冲完整流；不能把 SSE 摘要包装成 JSON 并声称是完整原始响应。
Strict 缓冲会推迟首段内容到达时间。

HTTP over UDS 与远端 HTTP(S) 使用相同 DTO。UDS 不替代 bearer 认证。
`examples/context_protection/external_executor.py` 给出了外部执行器示例：
用检查结果控制一次准确 JSON 请求和响应，落实字节上限、取消、期限及本次调用的绑定关联。
执行侧必须保留批准的准确字节、遵守期限、拒绝后停止、传播取消，并记录失败或不确定发送的用量。
本功能不安装具体第三方网关插件或 Rust 网关。
执行器必须在本地保留每次调用对应的 request binding；无状态 Core 无法证明任意外部执行器实际发送了哪次获准请求。
对于不透明上游网关，保护只到实际观察的端点，不能证明其内部路由、保留策略、账号/区域物理隔离或隐藏重试。

## 如何核算 Token 和缓存

### 按操作配置的精确输入编码

`InferenceConfig.input_encoding` 按操作选择输入表示，未指定的操作默认 `off`。当前接入
`atomic_memory.extract`、`atomic_memory.reconcile`、`experience.generate` 和 `experience.incubate`；
未知或尚未接入的操作会在配置加载时拒绝。Experience 生成与孵化分别控制，Skill、Profile、Handoff、
Topic Memory 尚未接入本编码策略。

```python
InferenceConfig(input_encoding={
    "atomic_memory.extract": "shadow",
    "experience.generate": "shadow",
    "experience.incubate": "off",
})
```

Server 可通过 JSON 环境变量配置：

```bash
POWERCONTEXT_SERVER_INFERENCE_INPUT_ENCODING='{"experience.generate":"shadow"}'
```

旧的 `atomic_memory_input_encoding` 配置及
`POWERCONTEXT_SERVER_INFERENCE_ATOMIC_MEMORY_INPUT_ENCODING` 环境变量继续作为两个 Atomic Memory 操作的默认值；
显式操作配置优先，包括显式 `off`。配置序列化后，后台 worker 使用相同选择规则。
编码不修改 Source、Artifact、PreparedContext 或 Embedding 输入，也不增加辅助模型调用。

| 模式 | 发给模型的内容 | 本地诊断 |
| --- | --- | --- |
| `off` | 原来的紧凑 JSON | 不构造候选 |
| `shadow` | 原来的紧凑 JSON | 计算候选、还原校验和成本 |
| `active` | 合格且更小的候选，否则原文 | 记录采用或回退原因 |

`active` 是需要按目标模型和指令组合验收的试验能力。程序能够完整还原 JSON，不等于模型阅读编码后
质量不变或推理更便宜；真实用量可能增加，也可能因输出预算耗尽而无法交付。先用 `shadow` 观察代表性输入，
再显式运行真实 A/B，确认必需事实、引用、失败率及输入加输出的完整用量后，才考虑为对应部署启用。
每个操作需要独立验收；Atomic Memory 提取的结果不代表调和或 Experience 生成的质量。

首个 codec `shared-json-v1` 将严格相同的长正文、metadata 或结构值保存一次，在有序原树的每个出现位置保留引用。
例如同一个项目的 metadata 同时出现在正文投影、来源元信息和多条 Source 中，只共享值，不合并 Source。
不同记录的身份、时间、journal position、引用、顺序和重复出现仍然全部保留。
原树使用带类型的对象、数组和引用节点，用户自己的 `$ref` 字段不会被当作内部引用。

Atomic Memory 使用兼容的 `shared-json-v1`。Experience 的 `shared-json-v2` 进一步支持正文字符串内部的
规范 JSON，以及保留原始换行符的完全相同文本片段。它能处理不同 Source 身份包裹的重复内容，
同时保留每条 Source 的身份、片段顺序和出现次数。非规范 JSON 字符串不被重新格式化。
候选是否采用仍取决于完整表示的收益与还原检查；公共数据中没有合适重复时继续使用原文。

每次候选都必须还原成与原始阶段 JSON **完全相同的字符串**，包括键顺序、Unicode、代码、空白字符串、
`null`、布尔值、`1` 与 `1.0`。解码说明、值表和引用开销都计入相同的估算器；只有字节更少且
至少减少 32 个估算 Token、同时减少至少 5% 才能采用。字节或估算变小不保证供应商实际 Token 下降。
输入大小、深度、节点数、共享值数量和解码展开均有上限，超限或不支持时保留原表示。
未知自定义 Artifact Prompt 使用原表示，避免改变它依赖的输入字段契约。

Atomic Memory 的阶段预算和生成器使用相同的表示选择。Experience 保留原有证据数量/长度与请求限制，
完整模型请求仍由执行侧检查目标窗口；本配置不新增 Experience 专属阶段预算。
启用强制隐私保护时，先检查原始序列化文本及严格解码后的内容，包括字符串内嵌的 JSON 对象和数组。
候选遇到 JSON 解析、深度或大小限制时，可以回退到已经通过检查的原文。
发现禁止内容、重复 JSON 键或其他策略失败时拒绝调用，不能将这些失败转为表示回退。
这项内容检查不授予完整请求的放行资格，最终完整模型请求仍经过隐私和目标窗口检查；
回原文不构成放行，原文不安全或仍超预算时继续拒绝。
这里没有删除成功日志、截断代码、按模板合并不同日志，也不把压缩过的输入写回权威记录。

进程内可使用
`powercontext.builtin.inference.usage.collect_input_encodings()`
包围一次调用，读取最多 128 条无正文诊断。记录包含阶段、codec、模式、采用/回退原因、
原始/候选/实际选择的字节和估算 Token、估算器版本、共享值与引用次数；预算探测不重复记录。
`UsageReportingStructuredGenerator` 会把本次实际准备的报告附在同一次 `InferenceUsage.input_encodings` 中，
通过现有绑定的用量 reporter 归因；返回结果和带有已知 usage 的失败都保留这份关联。
其中的估算值与供应商报告的 `input_tokens` / `output_tokens` 分开，不能相加或互相替代。
未取得 usage 的失败仍保留未知，不生成零请求的计费记录；原始隐私预检阻断发生在编码前，不生成编码报告。
显式诊断收集器仍可在调用外使用，预算探测不会计入实际调用。
这些诊断只属于当前进程和调用上下文，不自动跨 worker 汇总。现有持久统计只汇总实际模型用量，
不存储编码明细，也不是完整费用账本。
评估时应分别覆盖重复 metadata、相同正文不同身份、短小唯一输入、中英混合及代码/JSON。

### 模型实际用量

原生缓存参数仍由最终 provider adapter 处理，复用现有 Pydantic AI model settings。
显式 TTL/retention 需要目标 profile 批准；正文、目标或相关协议头变化后重新检查。
稳定准备值有利于前缀复用，但不保证命中。供应商 cache-read/write 指标不证明物理隔离或延迟下降。

对同一个成功任务比较“每轮准备”“准确复用”和已有“一次准备”基线，
完整计入查询 Embedding、生成输入/输出、辅助调用、预热、失败和重试。
缓存读 Token 通常已包含在输入 Token 中，不能重复累加；缺失 usage 或价格保留未知。
输出上限参数不能证明供应商的硬计费上限；实际报告用量即使超过参数，也必须完整保留。
分别报告输入缩减、完整任务 Token/费用、质量、延迟。
已有 Atomic 证据正文去重和查询向量复用属于基线，不能算作本功能的新收益。

使用 `tests/e2e/rfc2086_real_acceptance.py --help` 查看主动运行的真实服务验收脚本。
脚本使用隔离测试身份并记录不含正文的 usage 证据。
常规回归也覆盖真实本机 HTTP/SSE 传输、独立 Server 进程的 UDS 和正常证书校验的 HTTPS 链路；
它们不等于具体第三方供应商兼容性认证。
