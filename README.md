# PaperAgent

PaperAgent 是一个面向论文阅读和研究方向探索的全栈 Agent 工作台。系统会抓取 Arxiv 指定板块论文，保存论文元数据、AI 总结、标签、Markdown 内容、聊天记录和用户偏好，并在前端提供三栏式阅读、PDF 预览、Markdown 预览、划线翻译、Paper Chat 和 Ace Chat 推荐。

## 功能简介

- Arxiv 抓取：支持手动抓取指定板块，数据库内已有同版本论文会跳过，检测到 v2、v3 等新版本会更新记录。
- PDF 转 Markdown：封装 PaddleOCR 官方异步 API，记录每日 OCR 页数额度，并按配置暴露 10 页左右切分策略。
- 论文分析：通过 DeepSeek 或兼容 OpenAI 的接口生成论文总结、分类和标签；LLM 请求带 SQLite 缓存，降低重复调用成本。
- 长期记忆：SQLite 保存论文、摘要、标签、用户偏好和 OCR 额度。
- 短期记忆：SQLite 保存聊天 session 和消息，掉线后可恢复历史记录。
- RAG 问答：解析后的 Markdown 会切块写入 SQLite FTS，Paper Chat 基于全文检索片段回答。
- Daily Paper：支持按领域抓取指定日期论文，下载 PDF，转 Markdown，写入 ChromaDB，并生成缩略版与详细版总结。
- 用户推荐：Ace Chat 会根据用户聊天内容、首页板块偏好和数据库论文进行推荐，可选 Brave Search 扩展外部搜索。
- 前端工作台：左侧栏目与额度，中间论文列表、总结、Markdown、PDF，右侧 Paper Chat 和 Ace Chat，左右两栏可折叠，支持深色模式和移动端布局。

## 技术栈

- 后端：FastAPI、SQLite、APScheduler、httpx
- 前端：React、Vite、TypeScript、原生 CSS
- 外部服务：Arxiv Atom API、PaddleOCR API、DeepSeek 或 OpenAI 兼容 LLM、Brave Search API

## 目录结构

```text
paper-agent/
  server/                  # FastAPI 后端
    app/core/              # 配置、错误、日志和安全响应头
    app/db/                # SQLite 连接和 schema
    app/features/          # papers、chat、users、tools
  client/                  # React/Vite 前端
  docker-compose.yml
```

## 本地开发

### 一键启动

```bash
cd paper-agent
./start.sh
```

该脚本会使用 `server/.venv/bin/python` 启动后端，并同时启动 Vite 前端。默认地址：

```text
前端：http://localhost:5173
后端：http://localhost:8000
```

可通过环境变量覆盖端口或 host：

```bash
SERVER_PORT=8001 CLIENT_PORT=5174 ./start.sh
```

### 1. 后端
```bash
cd paper-agent
cp server/.env.example server/.env
source server/.venv/bin/activate
python -m pip install -r server/requirements.txt
cd server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

如果本地还没有虚拟环境，再创建：

```bash
cd paper-agent/server
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

说明：

- 当前 `requirements.txt` 已与 `chromadb==1.0.9` 对齐，使用 `fastapi==0.115.9`。
- 如遇到某些 AI 依赖在 `Python 3.13` 下缺少可用 wheel，建议改用 `Python 3.11` 或 `3.12` 重建 `server/.venv`。

后端启动后检查：

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

### 2. 前端

```bash
cd paper-agent/client
cp .env.example .env
npm install
npm run dev
```

浏览器访问：

```text
http://localhost:5173
```

## 关键配置

在 `server/.env` 中配置：

```dotenv
DATABASE_PATH=./data/paper_agent.sqlite3
STORAGE_ROOT=./data/storage

LLM_PROVIDER=deepseek
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=replace-with-your-key
LLM_MODEL=deepseek-chat
LLM_INTERFACE=chat_completions

PADDLEOCR_TOKEN=replace-with-your-token
PADDLEOCR_DAILY_PAGE_LIMIT=20000
PADDLEOCR_CHUNK_PAGES=10

BRAVE_API_KEY=
DEFAULT_ARXIV_CATEGORIES=cs.AI,cs.CL,cs.CV,cs.GR,cs.LG,stat.ML
CRAWL_INTERVAL_MINUTES=720
```

在 `client/.env` 中配置：

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8000
VITE_USER_ID=local-user
```

说明：

- `PADDLEOCR_TOKEN` 不要写入代码仓库。
- `LLM_INTERFACE=chat_completions` 适合 DeepSeek 和大多数 OpenAI 兼容服务。
- 若要接 OpenAI Responses API，可把 `LLM_INTERFACE` 改为 `responses`，并设置对应 `LLM_BASE_URL` 和模型。

## 使用流程

1. 打开前端，选择左侧 Arxiv 板块。
2. 点击“手动抓取最新”，后端会从 Arxiv 写入或更新论文。
3. 选择论文后点击“AI 分析”，系统会生成总结、分类和标签。
4. 如需全文 Markdown，调用 `/api/papers/{paper_id}/ocr` 提交 OCR，再调用 `/api/papers/{paper_id}/ocr/{job_id}/poll` 轮询结果。
5. 在摘要或 Markdown 中划选文本，可进行翻译，也可在 Paper Chat 中基于选区提问。
6. 在 Ace Chat 中描述研究兴趣，系统会更新用户偏好并推荐数据库内论文，配置 Brave API 后会附加网页搜索结果。

## API 摘要

- `GET /health`：服务健康检查
- `GET /ready`：数据库可用性检查
- `GET /api/config`：前端公共配置
- `GET /api/papers`：论文列表
- `GET /api/papers/{paper_id}`：论文详情和 Markdown
- `POST /api/papers/crawl`：手动抓取 Arxiv
- `POST /api/papers/{paper_id}/analyze`：AI 总结与分类
- `POST /api/papers/{paper_id}/ocr`：提交 OCR 任务
- `POST /api/papers/{paper_id}/ocr/{job_id}/poll`：轮询并保存 OCR 结果
- `POST /api/papers/{paper_id}/translate`：划线翻译
- `GET /api/daily-papers`：Daily Paper 列表
- `GET /api/daily-papers/runs`：Daily Paper 任务列表
- `POST /api/daily-papers/runs`：创建 Daily Paper 生成任务
- `POST /api/chat/sessions`：创建聊天会话
- `POST /api/chat/sessions/{session_id}/messages`：发送消息
- `GET /api/users/recommendations`：获取推荐论文
- `GET /api/quota/ocr`：查看 PaddleOCR 今日额度

## Docker 部署

```bash
cd paper-agent
cp server/.env.example server/.env
cp client/.env.example client/.env
docker compose up --build -d
```

访问：

```text
前端：http://localhost:5173
后端：http://localhost:8000
```

持久化数据会写入：

```text
paper-agent/data/
```

## 生产部署建议

- 使用反向代理统一 HTTPS，并把前端 `VITE_API_BASE_URL` 指向公网后端地址。
- 将 `server/.env` 放入部署平台的 Secret 管理系统，不要提交真实 token。
- 为 `/api` 增加正式 JWT 或 session 鉴权；当前 `X-User-Id` 只适合单机研究环境。
- 为 OCR 轮询增加后台任务队列，避免人工调用 poll。
- 若论文库规模增大，可将 SQLite FTS 扩展为向量库或 Postgres + pgvector。

## 校验

当前已通过：

```bash
cd paper-agent/server
.venv/bin/python -m compileall app
.venv/bin/python -c "from app.db.connection import init_db; init_db(); from app.main import health, ready; print(health()); print(ready())"

cd ../client
npm run build
```

如果运行环境无法访问 npm registry 或 PyPI，需要使用内部镜像安装依赖。
