# Day 4:Paper Ace Paper 多 Agent 改造说明（HR/面试视角）

## 1. 项目一句话介绍

Paper Ace Paper 是一个面向论文阅读、论文检索、研究灵感发现和研究方向推荐的全栈多 Agent 工作台。它把原来的两个聊天入口 `Paper Chat` 和 `Ace Chat` 合并成一个统一入口，并在后端用 6 个固定职责的 Agent 协作处理用户请求，前端实时展示当前使用的 Agent 和工具。

如果 HR 问“这个项目是做什么的”，可以这样回答：

> 这是一个 AI 论文研究助手。用户可以选择一篇论文，基于论文全文、数据库、arXiv、网页搜索和个人偏好来提问。系统会自动调度 Research、Summary、Inspiration、Suggestion、Tool Maker、Evaluation 六个 Agent，帮助用户检索证据、总结内容、发现创新点、推荐方向，并检查答案是否有来源。

## 2. 我这次主要做了什么

本次改造重点有 4 件事：

1. 合并聊天入口

原来系统有两个聊天模式：

- `Paper Chat`：偏论文内 RAG 问答。
- `Ace Chat`：偏工具调用、推荐、网页搜索。

现在统一成一个入口：

- `Paper Ace Paper`

所有聊天请求都会走新的 `paper_ace` 模式。后端仍兼容旧的 `paper` 和 `ace` 参数，但会自动归一化成 `paper_ace`。

关键代码：

- `server/app/features/chat/service.py`
- `server/app/features/chat/router.py`
- `client/src/components/ChatPanel.tsx`
- `client/src/App.tsx`

2. 增加 6 个 Agent 的管理逻辑

我新增了 Agent 配置文件：

- `server/app/features/chat/agents.py`

这里定义了 6 个 Agent：

- Research Agent
- Summary Agent
- Inspiration Agent
- Suggestion Agent
- Tool Maker Agent
- Evaluation Agent

每个 Agent 都有固定的 `key`、`name`、`purpose` 和 `when_to_use`，方便后端调度，也方便前端展示。

3. 前端展示当前使用的 Agent 和工具

前端聊天区域现在会显示一个 Agent Dock，列出所有 6 个 Agent。当某个 Agent 被本轮请求选中时，会显示“工作中”或“已核验”等状态。

同时，原有工具调用卡片继续保留，例如：

- `search_database`
- `search_rag_database`
- `web_search`
- `arxiv_search`
- `add_to_favorites`
- `shell_execute`

关键代码：

- `client/src/components/ChatPanel.tsx`
- `client/src/styles/app.css`
- `client/src/lib/api.ts`

4. 优化 DeepSeek Context Cache 的使用方式

DeepSeek 的 context cache 对“稳定前缀”更友好，所以我把多 Agent 的固定说明写成稳定 system prompt：

- `PAPER_ACE_AGENT_CHARTER`

运行时变化的信息，例如日期、用户 ID、当前论文、选区、附件论文、RAG 片段，会放在后续消息中。这样可以让 Agent 说明部分更容易命中缓存。

关键代码：

- `server/app/features/chat/agents.py`
- `server/app/features/chat/service.py` 中 `_paper_ace_initial_messages()`

## 3. 六个 Agent 的职责说明

### Research Agent

职责：

负责检索所有可用信息源，包括本地 SQL 论文库、已解析论文的 RAG chunks、arXiv 和网页搜索。

适用场景：

- 用户问某篇论文讲了什么。
- 用户问某个研究方向有哪些论文。
- 用户问最新论文或当前信息。
- 用户需要引用来源。

相关工具：

- `search_database`
- `search_rag_database`
- `arxiv_search`
- `web_search`

面试回答：

> Research Agent 是系统的信息获取层，它先查本地数据库和 RAG，如果证据不足，再查 arXiv 或网页。这样可以兼顾本地知识库速度和外部信息的新鲜度。

### Summary Agent

职责：

负责压缩长聊天历史、工具输出和多个 Agent 的结果，让上下文更短、更清晰。

适用场景：

- 用户要求总结。
- 会话历史很长。
- 工具结果很多，需要整理成短文本。

触发关键词示例：

- `summary`
- `summarize`
- `总结`
- `概括`
- `简短`

面试回答：

> Summary Agent 的价值是控制上下文长度，避免大模型被过多历史信息干扰，同时也降低 token 成本。

### Inspiration Agent

职责：

负责从论文方法、假设、实验和局限中发现创新点、研究空白和可继续深入的方向。

适用场景：

- 用户问“有什么研究灵感”。
- 用户问“这个方法能启发我做什么”。
- 用户问“还有什么创新点”。
- 用户问“future work / gap / novel idea”。

触发关键词示例：

- `idea`
- `inspire`
- `inspiration`
- `innovation`
- `novel`
- `future`
- `gap`
- `method inspire`
- `research on`
- `创新`
- `启发`
- `灵感`
- `方向`
- `不足`

这次用户反馈的问题：

用户输入：

```text
what can i make research on especially on the method inspire me.
```

之前 `select_agents()` 没有匹配 `inspire`，导致前端没有显示 Inspiration Agent。后来我补充了 `inspire`、`method inspire` 和 `research on` 这些触发词。

相关测试：

- `server/app/features/chat/test_agents.py`

面试回答：

> Inspiration Agent 是项目里最贴近研究创新的 Agent。它不是简单总结论文，而是把论文方法拆开，看哪些假设可以替换、哪些模块可以迁移、哪些实验缺口可以变成新课题。

### Suggestion Agent

职责：

根据用户偏好、聊天历史和已有论文库，为用户推荐可能感兴趣的论文和研究方向。

适用场景：

- 用户要求推荐论文。
- 用户要求下一步阅读路线。
- 用户提到自己的偏好。
- 用户想找研究方向。

触发关键词示例：

- `recommend`
- `suggest`
- `next`
- `reading`
- `推荐`
- `建议`
- `下一篇`
- `偏好`

面试回答：

> Suggestion Agent 是个性化推荐层。它不是只按关键词搜论文，而是结合用户历史偏好来推荐更可能值得读的内容。

### Tool Maker Agent

职责：

判断是否需要创建或调整工具、脚本或技能。它不会默认创建工具，只有当重复任务、复杂操作或高精度需求明显时才建议使用。

适用场景：

- 用户要求自动化。
- 用户要求创建工具。
- 用户要求脚本化某个流程。
- LLM 判断现有工具不足以完成任务。

触发关键词示例：

- `tool`
- `skill`
- `script`
- `自动化`
- `工具`
- `技能`
- `脚本`

面试回答：

> Tool Maker Agent 是系统的扩展能力，但我限制它不能随便造工具。它必须在确实有复用价值或能提高准确性的情况下才介入，避免系统复杂度失控。

### Evaluation Agent

职责：

检查最终回答是否有引用来源，是否存在证据不足、过度推断或不确定信息。

适用场景：

Evaluation Agent 默认每轮都会加入，因为论文研究场景需要可验证性。

面试回答：

> Evaluation Agent 是质量控制层。它保证输出不是“看起来很会说”，而是尽量有来源、有边界、有不确定性说明。

## 4. Agent 是怎么被选择的

选择逻辑在：

```text
server/app/features/chat/agents.py
```

核心函数是：

```python
select_agents(message: str, has_long_history: bool = False) -> list[AgentSpec]
```

基础规则：

- 每轮默认选择 `Research Agent`。
- 每轮最后默认加入 `Evaluation Agent`。
- 如果命中对应关键词，再加入 Summary、Inspiration、Suggestion 或 Tool Maker。
- 最后会去重，避免同一个 Agent 重复出现。

举例：

```python
select_agents("what can i make research on especially on the method inspire me.")
```

结果：

```python
["research", "inspiration", "evaluation"]
```

## 5. 后端调用流程

用户发送消息后，主要流程如下：

1. 前端调用流式接口

```text
POST /api/chat/sessions/{session_id}/stream
```

2. 后端进入：

```python
ChatService.stream_reply()
```

3. 请求模式被归一化：

```python
_normalize_mode()
```

旧模式：

- `paper`
- `ace`

都会变成：

- `paper_ace`

4. 进入新的统一 Agent 流程：

```python
_stream_paper_ace_ndjson()
```

5. 系统选择本轮 Agent：

```python
selected_agents = select_agents(message, has_long_history=len(session_history) >= 12)
```

6. 后端向前端发送 Agent 事件：

```json
{"type": "agent_start", "agentKey": "inspiration", "agentName": "Inspiration Agent"}
```

7. LLM 根据 prompt 决定是否调用工具。

8. 如果调用工具，后端向前端发送工具事件：

```json
{"type": "tool_start", "name": "search_database"}
{"type": "tool_result", "summary": "找到 8 篇相关论文"}
```

9. 最终生成答案并写入数据库。

## 6. 前端如何展示 Agent 和工具

前端类型定义在：

```text
client/src/lib/api.ts
```

新增了两类事件：

```ts
{ type: "agent_start"; agentKey: string; agentName: string; summary: string }
{ type: "agent_result"; agentKey: string; agentName: string; summary: string }
```

新增了 Agent 状态类型：

```ts
export type AgentActivity = {
  agentKey: string;
  agentName: string;
  status: "running" | "done";
  summary: string;
};
```

前端状态保存在：

```text
client/src/App.tsx
```

关键 state：

```ts
const [agents, setAgents] = useState<AgentInfo[]>([]);
const [agentActivitiesBySession, setAgentActivitiesBySession] = useState<Record<string, AgentActivity[]>>({});
```

聊天面板展示在：

```text
client/src/components/ChatPanel.tsx
```

核心组件：

```tsx
function AgentDock({ agents, activities }: { agents: AgentInfo[]; activities: AgentActivity[] })
```

样式在：

```text
client/src/styles/app.css
```

相关 class：

- `.agent-dock`
- `.agent-pill`
- `.agent-pill.running`
- `.agent-pill.done`

## 7. 如何运行项目

我新增了一键启动脚本：

```bash
./start.sh
```

它会调用：

```text
scripts/dev.sh
```

启动逻辑：

- 后端使用 `server/.venv/bin/python`
- 后端命令是 `python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`
- 前端进入 `client/` 后执行 `npm run dev`
- Ctrl-C 时会同时停止前端和后端

默认访问：

```text
前端：http://localhost:5173
后端：http://localhost:8000
```

如果要改端口：

```bash
SERVER_PORT=8001 CLIENT_PORT=5174 ./start.sh
```

## 8. 如何验证 Agent 是否工作

### 验证 Inspiration Agent

在聊天框输入：

```text
what can i make research on especially on the method inspire me.
```

预期前端 Agent Dock 中：

- Research 显示工作中或完成。
- Inspiration 显示工作中或完成。
- Evaluation 显示工作中或完成。

后端直接验证：

```bash
cd server
.venv/bin/python -c "from app.features.chat.agents import select_agents; print([a.key for a in select_agents('what can i make research on especially on the method inspire me.')])"
```

预期输出：

```text
['research', 'inspiration', 'evaluation']
```

### 验证 Summary Agent

输入：

```text
please summarize our discussion in simple words
```

预期包含：

```text
summary
```

### 验证 Suggestion Agent

输入：

```text
recommend me papers about video generation
```

预期包含：

```text
suggestion
```

### 验证 Tool Maker Agent

输入：

```text
can you make a script or tool to automate this paper search?
```

预期包含：

```text
tool_maker
```

## 9. HR 可能会怎么问，以及怎么答

### Q1：你为什么要把两个聊天入口合并？

回答：

> 原来的 Paper Chat 和 Ace Chat 对用户来说需要先判断该用哪个入口，增加了使用成本。实际上用户只关心“我问问题，系统帮我找论文、读论文、给灵感”。所以我把入口合并成 Paper Ace Paper，让系统自己判断该用 RAG、数据库、网页搜索还是推荐能力。

### Q2：你的多 Agent 系统是真多 Agent 吗？

回答：

> 目前它是一个轻量级多 Agent 编排系统。不是启动 6 个独立进程，而是在一个 LLM 调用链里定义 6 个明确职责的 Agent，并通过规则选择本轮参与的 Agent，同时把 Agent 状态流式展示到前端。这样实现成本低、响应快，也方便未来扩展成真正的并行 Agent。

### Q3：为什么不是每轮都调用所有 Agent？

回答：

> 因为每轮都调用所有 Agent 会增加 token 成本和延迟，也会让回答变得冗长。我设计为 Research 和 Evaluation 默认参与，其他 Agent 根据用户意图触发。例如用户问灵感时才触发 Inspiration，问推荐时才触发 Suggestion。

### Q4：这个系统怎么保证回答可靠？

回答：

> 第一，Research Agent 优先从本地数据库、RAG、arXiv 和网页工具获取证据。第二，Evaluation Agent 默认每轮参与，要求最终回答标明来源、不确定性和证据缺口。第三，工具调用结果会展示在前端，用户可以看到系统用了什么工具。

### Q5：DeepSeek Context Cache 你是怎么考虑的？

回答：

> DeepSeek 的缓存依赖稳定前缀。我把固定的 6 Agent 说明放在稳定 system prompt 中，把日期、用户 ID、当前论文和用户请求放在后面的消息中。这样重复对话时，Agent charter 更容易命中缓存，减少重复 prompt 成本。

### Q6：如果用户表达方式变化，Agent 会不会选错？

回答：

> 这是规则触发系统的风险。我已经通过用户反馈修复了一个例子：用户说 `method inspire me`，之前没有触发 Inspiration Agent，因为关键词里只有 `inspiration` 没有 `inspire`。我补充了触发词，并增加了回归测试。后续可以把规则触发升级成轻量 intent classifier。

### Q7：你怎么处理危险工具？

回答：

> `shell_execute` 工具会先做安全分类。安全命令可以直接执行，危险命令需要用户批准。前端会弹出审批窗口，用户批准后才执行。这避免了 LLM 自己执行破坏性命令。

### Q8：这个项目最能体现你能力的地方是什么？

回答：

> 我认为是把产品体验、后端编排、前端状态展示和 LLM 成本控制结合起来。不是只写一个 prompt，而是完整地做了 API、流式事件、前端 UI、工具调用、安全审批、缓存友好的上下文结构和文档。

## 10. 未来可以继续优化的方向

1. Agent 选择从关键词规则升级成 intent classifier。

现在 `select_agents()` 是规则匹配，简单直接，但对自然语言变化敏感。后续可以用小模型或 LLM 先判断用户意图，再决定 Agent。

2. 多 Agent 并行执行。

现在是一个 LLM 工具循环中体现多 Agent。后续可以让 Research、Inspiration、Suggestion 并行生成候选，再由 Evaluation 汇总。

3. Agent 记忆系统。

可以为每个 Agent 保存专属记忆，例如：

- Research 保存用户常搜领域。
- Suggestion 保存推荐反馈。
- Inspiration 保存用户偏好的创新风格。

4. 更强的引用检查。

Evaluation Agent 可以进一步要求每个关键 claim 都绑定到具体 tool result、paper id、arXiv id 或 URL。

5. 展示思考过程。

目前流式展示不是很明显，当调用工具完成时，容易看不到当前此时此刻Agent的输出内容，如果有思维链的话，需要展示Thinking并且可以展开给用户查看流式的思考输出


## 11. 本次相关文件清单

后端：

- `server/app/features/chat/agents.py`
- `server/app/features/chat/service.py`
- `server/app/features/chat/router.py`
- `server/app/features/chat/test_agents.py`
- `server/app/features/tools/llm.py`

前端：

- `client/src/App.tsx`
- `client/src/components/ChatPanel.tsx`
- `client/src/components/HistoryPanel.tsx`
- `client/src/lib/api.ts`
- `client/src/styles/app.css`

启动脚本：

- `start.sh`
- `scripts/dev.sh`

文档：

- `docs/multi-agent-chat.md`
- `notes/paper-ace-paper-agent-hr-note.md`

## 12. 面试时的 30 秒项目介绍

> 我做的是一个论文研究多 Agent 工作台。它把原本分散的论文问答和 Ace 工具聊天合并成一个 Paper Ace Paper 入口。后端定义了 6 个 Agent：Research 负责检索，Summary 负责压缩上下文，Inspiration 负责研究灵感，Suggestion 负责个性化推荐，Tool Maker 负责工具扩展，Evaluation 负责来源核验。前端会实时显示当前使用的 Agent 和工具。为了降低 LLM 成本，我把稳定的 Agent charter 放在固定 system prompt 中，让 DeepSeek context cache 更容易命中。整个项目包括 FastAPI 后端、React 前端、SQLite/RAG、arXiv/Web 搜索、工具审批和一键启动脚本。

## 13. 本次五项升级贡献补充

这次我在 Day 4 多 Agent 基础上继续做了 5 个工程升级，把原来“看起来像多 Agent”的实现，推进到更接近真实 Agent 编排系统。

### 13.1 Agent 选择从关键词规则升级为 Intent Classifier

原来的 `select_agents()` 主要依赖关键词匹配，例如用户输入里有 `recommend` 就触发 Suggestion Agent，有 `inspire` 就触发 Inspiration Agent。

这个方案简单，但问题是：

- 对自然语言变体敏感。
- 用户换一种说法可能就触发不到正确 Agent。
- 后续 Agent 数量变多时，规则会越来越难维护。

这次我新增了 intent classification 流程：

- 先让小模型或 LLM 判断用户意图。
- 输出结构化 JSON，包括 `primary_intent`、`intents`、`agent_keys`、`confidence` 和 `rationale`。
- 再根据分类结果选择 Agent。
- 如果没有配置 LLM API key，会回退到 deterministic fallback，保证本地开发和测试仍然可用。

关键代码：

- `server/app/features/chat/agents.py`
- `server/app/features/chat/service.py`
- `server/app/features/chat/test_agents.py`

面试回答：

> 我把 Agent 选择从硬编码关键词升级成了 intent classifier。这样系统先理解用户意图，再路由到 Research、Inspiration、Suggestion 等 Agent。为了保证开发环境稳定，我保留了离线 fallback，所以没有 LLM key 时也能跑测试和基本功能。

### 13.2 Research、Inspiration、Suggestion 支持并行执行

原来的多 Agent 更像是在一个 LLM 工具调用循环里模拟多个角色，所有事情都由同一个循环完成。

这次我把候选生成阶段拆成独立 Agent task：

- Research Agent 负责证据检索。
- Inspiration Agent 负责研究灵感和创新方向。
- Suggestion Agent 负责推荐候选。
- Summary 和 Tool Maker 在被选中时也可以独立生成候选。
- Evaluation Agent 最后统一汇总和核验。

实现方式：

- 后端使用 `asyncio.create_task()` 创建 Agent 任务。
- 使用 `asyncio.as_completed()` 接收先完成的 Agent 结果。
- 每个 Agent 有自己的 prompt、可用工具集合和候选输出。
- Evaluation Agent 接收所有候选结果后生成最终回答。

关键代码：

- `server/app/features/chat/service.py`

面试回答：

> 这次我把多 Agent 从单循环模拟升级成了并行候选生成。Research、Inspiration、Suggestion 可以同时工作，最后由 Evaluation Agent 聚合。这样架构上更清晰，也为以后接入真正独立的 Agent worker 打下基础。

### 13.3 增加每个 Agent 独立记忆系统

这次新增了 Agent Memory Store，每个 Agent 可以保存自己的长期记忆。

目前支持 3 类专属记忆：

- Research Agent：记住用户常搜索的 topic 和 source domain。
- Suggestion Agent：记住用户对推荐的正向、负向和中性反馈。
- Inspiration Agent：记住用户偏好的创新风格和 creative pattern。

数据库新增：

```sql
CREATE TABLE IF NOT EXISTS agent_memories (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  agent_key TEXT NOT NULL,
  memory_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(user_id, agent_key)
);
```

关键代码：

- `server/app/features/chat/memory.py`
- `server/app/db/schema.sql`
- `server/app/db/connection.py`
- `server/app/features/chat/service.py`

面试回答：

> 我没有把所有用户偏好混在一个全局 profile 里，而是按 Agent 职责拆了专属记忆。Research 记住常搜领域，Suggestion 记住推荐反馈，Inspiration 记住创新偏好。这样每个 Agent 只拿和自己任务相关的长期上下文，减少噪音。

### 13.4 加强 Evaluation Agent 的引用约束

论文研究场景最重要的是可验证性，所以这次我加强了 Evaluation Agent 的 citation enforcement。

新增机制：

- Candidate Agent 被要求每个 factual claim 都带来源标记。
- Evaluation Agent 会拿到可用 source refs，包括：
  - `paper_id`
  - `arXiv ID`
  - URL
  - 工具结果中的论文或网页引用
- 最终回答生成后，会做一次后处理检查。
- 如果某些较长事实陈述没有绑定到已知来源，会追加“引用检查”提示。

关键代码：

- `server/app/features/chat/service.py`
- `server/app/features/chat/test_citations.py`

面试回答：

> 我把 Evaluation Agent 从普通总结者升级成质量控制层。它不只是说“请引用来源”，而是显式收集可用 source refs，并检查最终回答里的关键事实是否绑定到 paper id、arXiv id 或 URL。缺少绑定的 claim 会被提示需要人工核验。

### 13.5 前端增加可展开的 Thinking 流

之前流式输出时，用户只能看到最终文本和工具卡片，中间 Agent 在做什么不够明显。

这次前端新增了 `Thinking` 区域：

- 后端新增 `thinking` stream event。
- 前端按 session 保存 thinking items。
- ChatPanel 中新增可折叠/展开的 Thinking 面板。
- 用户可以看到 intent classifier、各 Agent 候选输出摘要、Evaluation 汇总步骤。

这里展示的是可公开的 reasoning summary 和 agent progress，不展示模型原始隐藏 chain-of-thought。

关键代码：

- `client/src/lib/api.ts`
- `client/src/App.tsx`
- `client/src/components/ChatPanel.tsx`
- `client/src/styles/app.css`
- `server/app/features/chat/service.py`

面试回答：

> 我增加了 Thinking 流式面板，让用户能看到系统当前进入了哪个阶段，例如 intent classification、Research 候选生成、Evaluation 汇总。这里展示的是可解释的过程摘要，不暴露模型隐藏思维链，既提升透明度，也避免把内部推理原文直接暴露给用户。

## 14. 本次提交记录

这次改造按功能拆成了多个 focused commits：

- `3ab8ed3 Add intent classifier routing`
- `10a8155 Persist per-agent memory`
- `f263bb7 Run candidate agents in parallel`
- `63bfe51 Add citation enforcement coverage`
- `55cc297 Show live thinking stream`

这样拆分的好处：

- 每个改动可以独立回看。
- 出问题时容易定位是哪一层引入的。
- 面试或 code review 时能清楚说明演进路径。

## 15. 本次验证方式

后端验证：

```bash
python -m py_compile server/app/features/chat/agents.py server/app/features/chat/memory.py server/app/features/chat/service.py server/app/features/chat/test_agents.py server/app/features/chat/test_citations.py server/app/db/connection.py
```

Agent 路由测试：

```bash
cd server
python - <<'PY'
from app.features.chat.test_agents import (
    test_classifier_normalization_keeps_evaluation,
    test_inspiration_agent_selected_for_method_inspire_prompt,
    test_select_agents_uses_classifier_output,
)

test_inspiration_agent_selected_for_method_inspire_prompt()
test_select_agents_uses_classifier_output()
test_classifier_normalization_keeps_evaluation()
print('agent tests passed')
PY
```

引用检查测试：

```bash
cd server
python - <<'PY'
from app.features.chat.test_citations import (
    test_citation_report_accepts_known_url_refs,
    test_citation_report_flags_lines_without_known_refs,
)

test_citation_report_flags_lines_without_known_refs()
test_citation_report_accepts_known_url_refs()
print('citation tests passed')
PY
```

前端验证：

```bash
cd client
npm run build
```

注意：

- 当前环境没有安装 `pytest`，所以没有直接运行完整 `pytest -q`。
- 已通过 Python compile、直接调用测试函数和前端 production build 验证核心改动。

## 16. 更新后的 HR 版 30 秒介绍

> 我做的是一个论文研究多 Agent 工作台。最近我把它从关键词路由升级成 intent classifier，让系统先判断用户意图，再选择 Research、Inspiration、Suggestion 等 Agent。同时我把候选 Agent 改成并行执行，最后由 Evaluation Agent 聚合并检查引用。系统还新增了每个 Agent 的长期记忆，例如 Research 记住常搜领域，Suggestion 记住推荐反馈，Inspiration 记住创新偏好。前端新增了可展开的 Thinking 流，展示 Agent 当前进度和过程摘要。整体上，这次改造让项目从“角色化 prompt”更接近一个可解释、可验证、可持续扩展的多 Agent 研究系统。

## 17. 本轮生产化升级补充

这轮我继续把多 Agent 聊天从“功能可用”推进到“更接近生产级工程实现”，重点是 5 件事。

### 17.1 把编排层切到 LangGraph

之前虽然已经有 intent classifier、候选 Agent 和 Evaluation Agent，但核心流程还是我自己在 `service.py` 里串起来的。

这次我把核心聊天编排迁移到：

- `server/app/features/chat/workflow.py`

并用 LangGraph 的 `StateGraph` 明确表达工作流：

```text
classify -> candidate_batch* -> evaluate -> finalize
```

这里的价值是：

- 流程结构明确，不再依赖超长 service 文件硬编码。
- 每个阶段的输入输出都更清晰。
- 后面如果要加 checkpoint、human-in-the-loop 或更复杂分支，可以在 graph 上继续扩展。

面试回答：

> 我把聊天编排从手写 async 流程升级成 LangGraph 状态图，让 classify、candidate batches、evaluate、finalize 变成明确节点。这样代码的职责边界更清楚，也更接近生产里真正的 Agent orchestration。

### 17.2 把大文件拆成独立模块

原来的问题是 `service.py` 职责过重，同时承担路由后逻辑、调度、prompt、记忆、引用检查和工具循环。

这次我按职责拆成了：

- `agents.py`：Agent 注册、执行计划、分类结果模型
- `workflow.py`：LangGraph 编排
- `runtime.py`：超时、重试、预算、基础注入检测
- `conversation.py`：会话消息和引用检查相关逻辑
- `observability.py`：workflow 级资源使用聚合
- `workflow_store.py`：workflow / agent / tool / daily usage 持久化
- `prompts.py` + `prompts/*.md`：Prompt 外置和版本指纹

面试回答：

> 我没有继续往一个 service 文件里堆逻辑，而是把编排、prompt、运行时控制、观测和持久化拆成独立模块。这样不仅方便维护，也能让每个模块单独测试和演进。

### 17.3 增加运行时控制和预算机制

这轮新增了几类保护：

- workflow 超时
- 单 Agent 超时
- tool 调用超时
- LLM 重试和退避
- 单用户每日 token 预算
- 单用户每日 tool call 预算
- prompt injection 基础标记检测

对应配置已经放进：

- `server/app/core/config.py`

典型配置包括：

- `CHAT_WORKFLOW_TIMEOUT_SECONDS`
- `CHAT_AGENT_TIMEOUT_SECONDS`
- `CHAT_TOOL_TIMEOUT_SECONDS`
- `CHAT_DAILY_USER_TOKEN_BUDGET`
- `CHAT_DAILY_USER_TOOL_BUDGET`

面试回答：

> 多 Agent 系统最怕的是流程失控和成本失控，所以我加了超时、重试和预算控制。这样就算某个 Agent 一直想调工具，系统也会在配额和上限内停止，而不是无限循环。

### 17.4 增加可观测性和持久化运行记录

这轮我补了 workflow 运行轨迹落库，不再只靠临时日志看问题。

数据库新增了：

- `chat_workflow_runs`
- `chat_agent_runs`
- `chat_tool_runs`
- `chat_daily_usage`

它们分别记录：

- 一次聊天 workflow 的总体状态
- 每个 Agent 的耗时、token、状态、估算 cost
- 每个 tool call 的参数、结果、耗时和错误
- 每个用户每日 token 和 tool 使用量

同时，日志也统一成 structured event，例如：

- `chat_workflow_started`
- `chat_agent_completed`
- `chat_tool_completed`
- `chat_workflow_completed`

面试回答：

> 我把多 Agent 流程从“黑盒”变成了“可观测系统”。现在能看到每轮 workflow、每个 Agent、每次 tool call 花了多少时间、多少 token、有没有失败，这样线上问题才有办法定位。

### 17.5 Prompt 从代码常量迁到文件管理

原来 Agent charter、classifier prompt 和 evaluation prompt 直接写在 Python 常量里，改一次就得改代码和重启服务。

这次改成：

- `server/app/features/chat/prompts/*.md`

并通过：

- `server/app/features/chat/prompts.py`

统一加载、渲染和生成 prompt version 指纹。

这样做的好处是：

- prompt 更容易 review
- prompt 和代码职责分离
- 后面做 prompt 版本化、A/B 实验或外部管理更顺手

面试回答：

> 我把 prompt 从代码常量迁到独立文件，并给每份 prompt 生成版本指纹。这样后续做 prompt 迭代时，不需要再去翻 Python 代码找长字符串，也方便做版本追踪。

## 18. 这轮之后，系统架构和之前有什么本质区别

可以这样总结：

- 之前：多 Agent 能跑，但主要靠手写流程拼起来。
- 现在：多 Agent 已经有状态图编排、运行时约束、资源预算、持久化轨迹和外置 prompt，工程结构明显更稳定。

一句适合面试的表达：

> 这轮升级之后，项目不再只是“把几个角色 prompt 串起来”，而是变成了一个有工作流引擎、运行时控制、观测能力和可维护模块边界的多 Agent 研究系统。

## 19. 这轮验证方式

我这轮做了 3 类验证：

1. 编译检查

```bash
cd server
python -m compileall app
```

2. Agent execution plan 验证

```bash
cd server
python -c "from app.features.chat.agents import build_execution_plan, parse_intent_classification; c=parse_intent_classification('{\"primary_intent\":\"research\",\"agent_keys\":[\"research\",\"summary\",\"evaluation\"]}'); plan=build_execution_plan(c); print([[a.key for a in batch] for batch in plan.parallel_batches], plan.evaluation_agent.key)"
```

3. LangGraph 端到端 smoke test

我直接通过 `ChatService.reply()` 跑了一次真实调用，创建用户和 session，验证 LangGraph workflow 可以完整走通并返回答案。

注意：

- 当前环境没有安装 `pytest`
- 但已完成 Python compile、execution plan 断言和真实聊天 smoke test

## 20. 更新后的 HR 版 30 秒介绍

> 我最近把这个论文研究多 Agent 工作台继续往生产级推进。除了之前的 intent classifier、并行候选 Agent、引用检查和 Thinking 流，我又把编排层迁到了 LangGraph，用状态图明确管理 classify、candidate batch、evaluate 和 finalize 四个阶段。同时加入了超时、重试、token/tool 预算、运行轨迹落库和外置 prompt 管理。这样它不再只是几个 prompt 的组合，而是一个更可维护、可观测、可扩展的多 Agent 研究系统。
