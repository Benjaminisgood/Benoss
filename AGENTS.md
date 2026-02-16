# AGENTS.md - Benoss 项目 AI 后端与数据结构手册

## 1. 文档目标

本文件给 AI Agent 使用，目标是让 Agent 能在不依赖口头解释的情况下，快速理解 Benoss 的：

- 数据结构（数据库、归档 JSON、向量索引、OSS key）
- 后端实现（Flask 路由、权限、核心流程、AI 生成链路）
- 运行与维护方式（启动、配置、调试、常见改动点）

适用场景：

- 自动学习记录管理
- OSS 文件与资产管理
- 功能迭代与 bug 修复
- 自动化生成日报（博客/播客/海报）与向量问答

---

## 2. 项目定位与架构

Benoss 是一个 Flask 单体应用，核心能力：

- 用户发布学习记录（文本或文件）
- 记录可打标签、设 public/private、评论互动
- Notice 页面按筛选渲染整页内容
- 系统可按天自动生成公开日报资产（HTML 博客、播客音频、海报图片）
- 本地保存每日归档并构建向量索引，支持首页 RAG 问答

架构风格：

- Web 模板层：Jinja + 原生 JS
- API 层：Flask Blueprint (`site`, `account`, `api`)
- 数据层：SQLAlchemy + SQLite（默认）
- 对象存储层：阿里云 OSS（可选）/本地目录兜底
- AI 层：OpenAI 兼容接口（chat/embeddings/tts/images）

---

## 3. 代码目录地图（必须掌握）

```text
app/
  __init__.py               # create_app、建表、Schema 纠偏、管理员种子、Flask CLI
  config.py                 # 所有环境变量读取与默认值
  extensions.py             # db = SQLAlchemy()
  models.py                 # 数据模型与索引
  oss.py                    # OSS 远端 + 本地兜底实现
  routes/
    __init__.py             # 注册蓝图
    site.py                 # 页面路由
    account.py              # 账户 API + 管理员运行时配置 API
    api.py                  # 记录/评论/看板/Notice/日报/向量/资产核心逻辑
  utils/
    session_auth.py         # 登录态与鉴权装饰器
    runtime_settings.py     # 运行时配置定义、校验、持久化覆盖
    local_archive.py        # 每日归档 JSON（schema_version=2）
    local_vector_db.py      # 本地向量索引构建/检索
    oss_paths.py            # OSS key 规则
    ids.py                  # UUID
  cli/
    sync.py                 # benoss-sync 命令行工具

data/
  benoss.sqlite
  oss-local/
  uploads/
  daily-archive/
  vector-store/
```

---

## 4. 数据结构

## 4.1 数据库模型（`app/models.py`）

### `User`

- `id` int PK
- `username` varchar(80) unique
- `password_hash` varchar(255)
- `role` varchar(32), 默认 `user`（管理员为 `admin`）
- `is_active` bool
- `description` text
- `created_at`, `updated_at`（`TimestampMixin`）

方法：

- `set_password(password)`
- `check_password(password)`

### `AppSetting`

- `id` int PK
- `key` varchar(128) unique + index
- `value` text
- `created_at`, `updated_at`

用途：存储管理员在前端覆盖的运行时配置，优先级高于 `.env`。

### `Content`

- `id` int PK
- `kind` (`text` | `file`)
- `text_content` text
- `oss_key` varchar(512)
- `filename` varchar(255)
- `content_type` varchar(255)
- `size_bytes` int
- `sha256` varchar(64)
- `created_at`, `updated_at`

### `Record`

- `id` int PK
- `user_id` FK -> `User.id` + index
- `content_id` FK -> `Content.id` + unique + index（1 记录对应 1 内容）
- `format` varchar(32)
- `visibility` (`public` | `private`)
- `tags_json` text（JSON 字符串）
- `preview` text
- `created_at`, `updated_at`

方法：

- `get_tags()`：安全解析 `tags_json`
- `set_tags(tags)`：去空、忽略大小写去重、保序，写回 JSON

### `Comment`

- `id` int PK
- `record_id` FK -> `Record.id` + index
- `user_id` FK -> `User.id` + index
- `body` text
- `created_at`, `updated_at`

关系：

- `Record.comments` 带 `cascade="all, delete-orphan"`（删记录时删评论）

### `GeneratedAsset`

- `id` int PK
- `user_id` FK -> `User.id` + index
- `kind` (`blog_html` | `podcast_audio` | `poster_image`)
- `title`
- `provider`, `model`
- `visibility` (`public` | `private`)
- `status`（默认 `ready`）
- `is_daily_digest` bool
- `source_day` date（日报归属日）
- `content_type`, `ext`, `size_bytes`, `oss_key`, `sha256`
- `source_filters_json` text
- `created_at`, `updated_at`

### `DailyDigestJob`

- `id` int PK
- `day` date + index
- `timezone`
- `status`（`running` | `ready` | `partial` | `failed`）
- `started_at`, `finished_at`, `error`
- `blog_asset_id`, `podcast_asset_id`, `poster_asset_id`（FK -> `GeneratedAsset.id`）
- `created_at`, `updated_at`

### 索引（模型中显式定义）

- `ix_record_user_created`
- `ix_record_visibility_created`
- `ix_comment_record_created`
- `ix_generated_asset_user_created`
- `ix_generated_asset_public_day_created`
- `ux_daily_digest_day_tz`（`day + timezone` 唯一）

---

## 4.2 关系图（文字版）

- `User 1:N Record`
- `Record 1:1 Content`
- `Record 1:N Comment`
- `User 1:N Comment`
- `User 1:N GeneratedAsset`
- `DailyDigestJob 1:0..3 GeneratedAsset`

---

## 4.3 归档 JSON 结构（`data/daily-archive/YYYY-MM-DD.json`）

`schema_version = 2`

顶层字段：

- `schema_version`
- `day`
- `scope`（通常 `public`）
- `source`（`home_today` 或 `daily_digest`）
- `timezone`
- `updated_at`
- `record_count`
- `records[]`

`records[]` 每项字段：

- `id`, `record_no`, `format`, `visibility`, `preview`, `tags[]`
- `created_at`, `updated_at`
- `user: {id, username}`
- `content`:
  - 文本：`{kind: "text", text, media_type: "text"}`
  - 文件：`{kind: "file", filename, content_type, media_type, size_bytes, sha256, oss_key}`
- `extraction`: `{status, text, encoding, truncated, bytes_read, message}`
- `text`（该记录用于下游检索/生成的归一化文本）

---

## 4.4 本地向量索引结构（`data/vector-store/index.json`）

当前目标结构（`local_vector_db.py`）：

- `schema_version`（当前实现常量为 `2`）
- `built_at`, `updated_at`
- `archive_count`, `doc_count`
- `vector_dim`
- `embedding: {provider, model}`
- `documents[]`

`documents[]` 每项字段：

- `id`（`{day}:{record_id}`）
- `day`, `record_id`, `user_id`, `username`
- `tags[]`, `created_at`, `preview`, `text`
- `content_hash`
- `vector`（dense float list）
- `vector_dim`, `vector_norm`

兼容说明：

- 旧索引文件若不符合 schema，`_load_index()` 会视为无效并触发重建流程。

---

## 4.5 CLI 配置文件结构（`.benoss/config.json`）

版本：`version = 3`

字段：

- `version`
- `base_url`
- `default_tag`

---

## 5. 配置体系与优先级

来源优先级（高 -> 低）：

1. `AppSetting`（管理员页面保存）
2. `current_app.config`（来自 `.env` / 环境变量）
3. `runtime_settings.py` 中定义的 default

关键函数：

- `get_setting_str/int/bool`
- `admin_settings_payload()`
- `save_admin_settings(values, reset_keys)`

AI provider 别名归一化：

- `open_ai` / `open-ai` -> `openai`
- `chat_anywhere` / `chat-anywhere` -> `chatanywhere`
- `dashscope` -> `aliyun`

当前版本 provider 主键：

- 使用 `AI_PRIMARY_PROVIDER` 作为全局主 provider（聊天 + 向量默认均跟随）。
- `AI_TTS_PROVIDER` / `AI_IMAGE_PROVIDER` 为空时跟随 `AI_PRIMARY_PROVIDER`，不再使用旧键兼容逻辑。

能力分流执行顺序（`app/routes/api.py` + `app/utils/local_vector_db.py`）：

1. 聊天能力：
   - `_ai_provider_settings()` 只读取 `AI_PRIMARY_PROVIDER` 对应配置，不做 provider 自动切换。
2. 向量 embedding：
   - `_embedding_provider_settings()` 只读取 `AI_PRIMARY_PROVIDER` 对应 provider 的 `*_EMBEDDING_MODEL`。
3. TTS/图片能力：
   - `_capability_settings_candidates()` 的 provider 顺序为：
     - `AI_TTS_PROVIDER`/`AI_IMAGE_PROVIDER`（若设置）
     - `AI_PRIMARY_PROVIDER`
     - 固定备用序：`openai -> chatanywhere -> aliyun -> deepseek`（去重后尝试）
   - model 顺序为：
     - 每个 provider 只读取自己的 `*_TTS_MODEL`/`*_IMAGE_MODEL`（排除 `unsupported/none` 等占位符）
   - 结论：只要备用 provider 的 key/base_url 完整，TTS/图片就可能在备用 provider 上成功，不会强制停留在主 provider。

---

## 6. 鉴权与权限模型

实现：`app/utils/session_auth.py`

- 登录成功后在 session 写入：`user_id`, `username`, `role`
- `before_request` 通过 `load_current_user()` 注入 `g.user`
- `login_required()`:
  - API 未登录返回 `401 {"error":"login required"}`
  - 页面未登录跳转 `/login`
  - 角色不匹配 API 返回 `403 {"error":"forbidden"}`

可见性规则核心：

- `public` 记录：所有登录用户可见
- `private` 记录：仅作者可见
- `GeneratedAsset`：`public` 对登录用户可见，`private` 仅作者可见

---

## 7. 对象存储（OSS）策略

实现：`app/oss.py`

远端模式条件：

- `OSS_ENDPOINT`, `OSS_ACCESS_KEY_ID`, `OSS_ACCESS_KEY_SECRET`, `OSS_BUCKET` 全部存在

否则走本地目录：

- `OSS_LOCAL_DIR`（默认 `data/oss-local`）

统一接口：

- `put_object_from_file`
- `put_object_bytes`
- `get_object_bytes`
- `delete_object`
- `copy_object`
- `sign_get_url`
- `public_url`

OSS key 规则（`app/utils/oss_paths.py`）：

- 记录文件：`{prefix}/records/{YYYY-MM-DD}/objects/{uuid}.{ext}`
- 生成资产：`{prefix}/generated/{YYYY-MM-DD}/user-{id}/{kind}/{uuid}.{ext}`

---

## 8. 应用启动与后台任务入口

入口：`app/__init__.py`

`create_app()` 过程：

1. `app.config.from_object(Config)`
2. `db.init_app(app)`
3. 注册蓝图
4. `db.create_all()`
5. `_ensure_schema_shape()`（修正旧表结构，必要时重建）
6. `_seed_admin()`（按环境变量创建/提升管理员）
7. 注册 `/health`
8. 注入 `g.user`

Flask CLI 命令：

- `flask --app app init-db`（drop + create + seed admin）
- `flask --app app digest-build [--day --timezone --force]`
- `flask --app app vector-build [--max-docs --force]`

---

## 9. API 总览（后端主实现）

## 9.1 页面路由（`site.py`）

- `GET /`
- `GET /board`
- `GET /echoes`
- `GET /notice`
- `GET /admin`（admin）
- `GET/POST /login`
- `GET/POST /register`
- `GET /logout`

## 9.2 账户与管理路由（`account.py`）

- `GET /api/account`
- `PATCH /api/account/description`
- `GET /api/users`
- `GET /api/admin/settings`（admin）
- `PUT /api/admin/settings`（admin）

## 9.3 核心业务路由（`api.py`）

- `POST /api/push`（别名）
- `GET /api/pull`（别名，默认返回 content）
- `POST /api/records`
- `GET /api/records`
- `GET /api/records/<id>`
- `PATCH /api/records/<id>`
- `DELETE /api/records/<id>`
- `GET /api/records/<id>/comments`
- `POST /api/records/<id>/comments`
- `GET /api/contents/<id>/blob`
- `GET /api/board`
- `GET /api/board/cell`
- `GET /api/board/user/<id>/records`
- `GET /api/board/date/<day>`
- `GET /api/echoes`
- `GET /api/notice/render`
- `GET /api/generated-assets`
- `POST /api/digest/daily`（admin）
- `GET /api/generated-assets/<id>/blob`
- `GET /api/home/today`
- `POST /api/vector/rebuild`
- `POST /api/vector/chat`

---

## 10. 关键业务流程（必须理解）

## 10.1 发布记录（文本/文件）

入口：`POST /api/push` 或 `POST /api/records`

流程：

1. 解析 JSON/Form 参数
2. `tags` 清洗（最多 20，单 tag 最多 40 字符）
3. visibility 归一化（默认 `private`）
4. 如果有 `file`：
   - 存临时目录 `data/uploads`
   - 算 `sha256`
   - 上传 OSS/本地对象目录
   - 生成 `Content(kind=file)`
5. 否则要求 `text` 非空，生成 `Content(kind=text)`
6. 创建 `Record`，计算 `preview`，推断/应用 `format`
7. DB commit

---

## 10.2 拉取记录与可见性过滤

统一查询入口：`_record_query_for(user, public_only=...)`

- `public_only=False`：公开 + 自己私密
- `public_only=True`：仅公开

筛选器：`_apply_filter_values()`

- `user_id`
- `tag`（`tags_json.contains(...)`）
- `day`（`YYYY-MM-DD`）

---

## 10.3 更新与删除记录

更新：`PATCH /api/records/<id>`

- 仅作者可改
- 可改 `visibility/tags/format`
- 文本记录可改 `text`（不可空）
- 上传新文件时会替换 `Content` 文件字段并尝试删除旧 OSS 文件

删除：`DELETE /api/records/<id>`

- 仅作者
- 删除 `Record` + 对应 `Content`
- 若文件记录，提交后尽力删除 OSS 对象

---

## 10.4 评论

- `GET /api/records/<id>/comments`
- `POST /api/records/<id>/comments`

规则：

- 仅能对“自己可见”的记录评论
- 评论 body 非空，长度 <= 2000

---

## 10.5 Notice 渲染与 AI 资产生成

纯渲染：

- `GET /api/notice/render`
- 使用 `_render_notice_html()` 生成整页 HTML 字符串（不落库）

日报/资产相关：

- 资产记录在 `GeneratedAsset`
- 资产二进制放 OSS
- `GET /api/generated-assets` 查询
- `GET /api/generated-assets/<id>/blob` 读取

AI 生成链路：

1. 先构造记录上下文（优先从归档 `records[].text`）
2. 博客：`_generate_blog_asset()` -> `chat/completions` -> 包装 HTML 文档
3. 播客：`_generate_podcast_asset()` -> 先生成脚本再走 `/audio/speech`
4. 海报：`_generate_poster_asset()` -> 先生成图像提示词再走 `/images/generations`
5. TTS/图片会经 `_capability_settings_candidates()` 做候选 provider/model 路由（分流 provider -> 主 provider -> 备用 provider）
6. 若外部能力不可用：
   - `AI_TTS_FALLBACK_LOCAL=1` 时，播客音频降级到本机 `say`（通常输出 `.aiff`）
   - `AI_IMAGE_FALLBACK_LOCAL=1` 时，海报降级为本地 SVG
7. `_save_generated_asset()` 保存文件与数据库元数据

---

## 10.6 每日公开日报（Digest）

主函数：`build_daily_public_digest(day_value, force, timezone_name)`

流程：

1. 以 `day + timezone` 创建或更新 `DailyDigestJob`，置 `running`
2. 查询该本地日窗口内公开记录（按 timezone 转换为 UTC 边界）
3. 归档并按需重建向量索引（`_archive_and_index_records`）
4. 准备 AI 输入文本和图片附件 URL
5. 检查是否已有同日 `ready` 资产（`force=False` 时复用）
6. 分别生成 `blog_html/podcast_audio/poster_image`
7. 根据成功数把任务状态设为 `ready/partial/failed`
8. 写回资产关联 ID 和错误信息

---

## 10.7 Home 今日接口（自动化入口）

接口：`GET /api/home/today`

行为：

1. 计算 digest timezone 下的“今天”
2. 查询今天公开记录
3. 保存本地归档并触发可选向量自动重建
4. 返回向量索引状态（必要时自动 `ensure_index`）
5. 根据配置尝试自动补齐今日日报资产（可限流重试）
6. 返回 `public_records + today_assets + digest_build + ai + archive + vector`

这是前端 Home 页和自动学习/管理流程的核心汇总接口。

---

## 10.8 向量检索与问答

- 重建：`POST /api/vector/rebuild`
- 问答：`POST /api/vector/chat`

检索流程：

1. 从本地归档提取公开记录文档
2. 对新增/更新文档做 embedding upsert（增量）
3. 正常路径：本地 dense vector + cosine 相似度
4. 若 embedding/索引异常：自动降级为基于归档文本的 lexical 检索（`meta.retrieval_mode=lexical_fallback`）
5. 返回 `citations`（带 day/record_id/score/snippet），结构保持不变

回答流程：

- `use_ai=true` 且 provider 可用时，用检索命中构造上下文调用 `_ai_chat`
- 否则回退 `_vector_chat_fallback_answer`
- 强约束提示词：只基于检索结果，不足则明确说明证据不足

---

## 11. 运行与运维命令

推荐脚本：

```bash
./benoss.sh bootstrap
./benoss.sh start
./benoss.sh status
./benoss.sh logs
```

手动：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
python -m flask --app app init-db
python -m flask --app app run --debug --port 80
```

辅助命令：

```bash
python -m flask --app app digest-build
python -m flask --app app vector-build
benoss-sync init --base-url http://127.0.0.1:80
benoss-sync status --username <user>
benoss-sync push --username <user> --text "hello"
benoss-sync pull --username <user> --output ./pulled_records
```

补充：

- 若 `digest-build` 在 CLI 报 `Unable to build URLs outside an active request`，优先改走 `POST /api/digest/daily`（admin）触发同等流程。

---

## 12. AI 改代码时的硬性约束

### 12.1 改模型时

- 修改 `app/models.py` 后，检查 `_ensure_schema_shape()` 是否仍兼容
- 若新增表或关键字段，补齐：
  - schema 纠偏逻辑
  - payload 序列化逻辑
  - API 查询过滤逻辑

### 12.2 改设置项时

- 在 `SETTING_DEFINITIONS` 增加定义
- 确认 `type/min/max/options/default` 合法
- 如涉及 provider 名称，补别名归一化

### 12.3 改记录与文件流程时

- 保持 `Record` 与 `Content` 的 1:1 关系
- 文件替换/删除必须尽力清理 OSS 旧对象
- 任何可见性相关逻辑必须复用 `_visible_filter/_is_record_visible`

### 12.4 改 AI 生成流程时

- 失败必须返回可诊断错误，不吞异常
- 长文本必须走截断策略（总长度与单条长度双限制）
- 图片附件要尊重 `AI_NOTICE_ATTACH_IMAGES` 和数量上限

### 12.5 改向量流程时

- 保持增量 upsert 语义（通过 `content_hash` 判断是否重算 embedding）
- 兼容空索引/坏索引自动重建
- 搜索返回字段不要破坏 `citations` 前端消费结构

---

## 13. 常见故障定位

- `AI provider not configured`：
  - 检查 `AI_PRIMARY_PROVIDER` 与对应 API KEY/base_url/`*_CHAT_MODEL`
- `embedding provider not configured`：
  - 向量重建依赖 provider + 对应 provider 的 `*_EMBEDDING_MODEL`
- `embedding request failed (404) ... model_not_found`：
  - 先检查生效配置（含 AppSetting 覆盖）：`AI_PRIMARY_PROVIDER` 与该 provider 的 `*_EMBEDDING_MODEL`
  - 该报错通常表示“模型名与当前 provider 不匹配”，不是本地向量库损坏
  - 修正模型后执行 `python -m flask --app app vector-build --force` 重新构建
- Home 没看到手动生成资产：
  - Home `today_assets` 仅展示 `public + is_daily_digest=true + source_day=今天` 的资产
  - 手动脚本默认常用 `visibility=private`，不会出现在 Home 卡片
- `content unavailable` / `asset unavailable`：
  - 检查 `oss_key`、OSS 凭据或本地 `data/oss-local` 文件
- Digest 长期 `partial/failed`：
  - 查看 `daily_digest_job.error`
  - 若错误出现在 `chat/completions` 阶段，博客/播客/海报都会受影响（脚本/提示词前置失败）
  - 若错误仅在 TTS/图片阶段，检查 provider 能力、余额与本地兜底开关

---

## 14. 给 AI Agent 的推荐执行顺序（学习/OSS 管理）

1. 先调用 `/api/home/today` 获取当天全局状态。
2. 若要学习记录，调用 `/api/pull` 或 `/api/records`（带筛选）。
3. 若要问答，先保证 `/api/vector/rebuild` 可成功，再用 `/api/vector/chat`。
4. 若要日报资产，管理员调用 `/api/digest/daily`。
5. 若要做 OSS 清理或稽核，结合 `Record/GeneratedAsset.oss_key` 与对象目录做比对。

---

## 15. 关键事实速记

- 默认 DB：`data/benoss.sqlite`
- 默认最大上传：1GB（`MAX_CONTENT_LENGTH`）
- 所有时间归档按 `DIGEST_TIMEZONE` 切日
- 归档是事实来源之一，向量索引由归档构建，不直接扫数据库
- Home 页面是自动化入口：会触发归档、向量状态检查、可选日报自动补齐
- Home 的 `today_assets` 不等于“今天新生成的所有资产”，只展示公开且标记为日报产物的资产
