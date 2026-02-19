# Benoss

Benoss 是一个“学习记录流”应用：用户可以发布文本或文件记录，按标签筛选、评论互动；Notice 页面用于筛选并拼接整页内容，系统会在自然日结束后，自动基于上一日公开内容生成博客、播客和海报并展示到 Home。

项目当前是一个完整的 Flask 单体应用（Web 页面 + API + 数据库 + 对象存储 + AI 调用）。

## 1. 现在能做什么

- 账号系统：注册、登录、退出、会话登录态。
- 记录系统：
  - 发布文本记录或上传文件记录；
  - 每条记录可设置 `public/private`；
  - 可打标签（如 `math,python`）；
  - 作者可编辑或删除自己的记录。
- 评论系统：对“你有权限看到”的记录发表评论。
- 看板视图：
- `Home`：今天公开记录概览 + 快速发布 + 已闭合日自动生成资产；
  - `Board`：按用户 × 日期统计热力表 以及记录查看；
  - `Echoes`：公开内容流；
  - `Notice`：按筛选条件拼接整页内容（不含页面内 AI 生成入口）。
- 资产生成（可选）：
  - AI 生成博客网页（html）；
  - AI 生成播客音频（本地脚本 + TTS，支持对话/演讲/访谈/播报风格）；
  - AI 生成海报图片（png，失败时可本地兜底为 svg）。
  - 自动按天保存本地归档（JSON），向量库采用本地持久化 + embedding 增量 upsert。
  - Home 页内置本地向量问答机器人（RAG 检索 + 可选 AI 回答）。

## 2. 技术栈与运行形态

- 后端框架：Flask（`app/__init__.py`）
- ORM：Flask-SQLAlchemy（`app/extensions.py`, `app/models.py`）
- 数据库：默认 SQLite（`data/benoss.sqlite`）
- 对象存储：
  - 远端 OSS（阿里云 OSS 兼容）
  - 或本地落盘目录（`data/oss-local/`）
- 前端：Jinja 模板 + 原生 JS（`app/templates/*`, `app/static/js/site.js`）
- AI 接入：
  - OpenAI 兼容 API（博客/播客脚本/向量问答/海报，通过 `requests` 调用）
  - TTS 接口（播客音频）

## 3. 代码目录（先看这个）

```text
app/
  __init__.py              # 应用创建、初始化数据库、注入当前用户、健康检查
  config.py                # 全部环境变量配置（数据库/OSS/AI/会话等）
  extensions.py            # db = SQLAlchemy()
  models.py                # 核心数据模型（User/Content/Record/Comment/GeneratedAsset/DailyDigestJob）
  oss.py                   # 对象存储抽象（远端 OSS + 本地文件兜底）
  routes/
    site.py                # 页面路由：/ /board /echoes /notice /login /register
    account.py             # 账户相关 API：当前用户、用户列表、简介更新
    api.py                 # 核心业务 API：记录、评论、board、notice、AI、资产
  utils/
    session_auth.py        # 登录态读取、登录校验装饰器
    oss_paths.py           # OSS key 命名规则
    ids.py                 # UUID 生成
    local_archive.py       # 本地按天归档（JSON）
    local_vector_db.py     # 本地向量索引与检索
  cli/
    sync.py                # benoss-sync 命令行工具（pull/push/status）
data/
  benoss.sqlite            # 默认数据库文件
  oss-local/               # 未配置远端 OSS 时的本地对象存储目录
  uploads/                 # 文件上传临时目录
  daily-archive/           # 本地按天归档目录
  vector-store/            # 本地向量索引目录
```

## 4. 数据结构（重点，面向小白）

数据模型定义在 `app/models.py`。可以把它理解成 5 张核心表：

### 4.1 `User`（用户表）

字段：
- `id`：主键。
- `username`：唯一用户名。
- `password_hash`：密码哈希，不存明文。
- `role`：角色，默认 `user`，可有 `admin`。
- `is_active`：是否可登录。
- `description`：个人简介。
- `created_at`, `updated_at`：创建/更新时间（来自 `TimestampMixin`）。

关键方法：
- `set_password(password)`：生成哈希。
- `check_password(password)`：校验登录密码。

### 4.2 `Content`（内容实体表）

`Record` 不直接存大内容，而是关联一个 `Content`。这样文本和文件能统一处理。

字段：
- `id`：主键。
- `kind`：`text` 或 `file`。
- `text_content`：文本内容（当 `kind=text` 时使用）。
- `oss_key`：文件在对象存储中的路径 key（当 `kind=file` 时使用）。
- `filename`, `content_type`, `size_bytes`, `sha256`：文件元信息。
- `created_at`, `updated_at`。

### 4.3 `Record`（记录主表）

这是业务主对象，页面里看到的“每一条记录”就是它。

字段：
- `id`：主键（也作为记录号）。
- `user_id`：记录作者。
- `content_id`：关联 `Content`，并且 `unique=True`，表示“一条记录对应一个内容实体”。
- `visibility`：`public` 或 `private`。
- `tags_json`：标签 JSON 字符串（例如 `["math","python"]`）。
- `preview`：预览文本。
- `created_at`, `updated_at`。

标签处理（非常重要）：
- `set_tags()` 会做清洗：
  - 去空；
  - 忽略大小写去重；
  - 保留输入顺序；
  - 最终存成 JSON 字符串到 `tags_json`。
- `get_tags()` 负责把 `tags_json` 安全解析为列表。

### 4.4 `Comment`（评论表）

字段：
- `id`
- `record_id`：评论属于哪条记录。
- `user_id`：评论作者。
- `body`：评论正文。
- `created_at`, `updated_at`。

关系：
- `Record.comments` 使用 `cascade="all, delete-orphan"`，删除记录时评论会一起删掉。

### 4.5 `GeneratedAsset`（AI 生成资产表）

用于保存 Notice 页与日报任务产出的博客/音频/图片等文件元信息。

字段：
- `id`
- `user_id`：哪个用户生成的。
- `kind`：如 `blog_html` / `podcast_audio` / `poster_image`。
- `title`：资产标题。
- `provider`, `model`：生成所用模型信息。
- `visibility`：`public/private`。
- `status`：生成状态，默认 `ready`。
- `is_daily_digest`：是否日报自动产物。
- `source_day`：资产归档日期（可为空）。
- `content_type`, `ext`, `size_bytes`, `oss_key`, `sha256`：文件信息。
- `source_filters_json`：当时使用的筛选条件（JSON）。
- `created_at`, `updated_at`。

### 4.6 `DailyDigestJob`（日报任务表）

用于记录每天公开内容的自动汇总任务状态与产物关联。

字段：
- `day`：汇总哪一天（按 `DIGEST_TIMEZONE` 切分）。
- `timezone`：任务所用时区。
- `status`：`running/ready/partial/failed`。
- `started_at`, `finished_at`, `error`：任务执行信息。
- `blog_asset_id`, `podcast_asset_id`, `poster_asset_id`：关联 `GeneratedAsset`。

### 4.7 模型关系图（文字版）

- `User 1 -> N Record`
- `Record 1 -> 1 Content`
- `Record 1 -> N Comment`
- `User 1 -> N Comment`
- `User 1 -> N GeneratedAsset`
- `DailyDigestJob 1 -> 0..3 GeneratedAsset`

## 5. 后端请求是怎么跑起来的

### 5.1 应用启动：`app/__init__.py`

`create_app()` 做了这些事：

1. 加载 `Config`。
2. 初始化 `db`。
3. 注册蓝图（`site/account/api`）。
4. 在应用上下文中：
   - `db.create_all()` 建表；
   - `_ensure_schema_shape()` 做表结构校验；
   - `_seed_admin()` 按环境变量创建或升级管理员账号。
5. `before_request` 把当前会话用户挂到 `g.user`。
6. 提供 `/health` 健康检查。

### 5.2 登录态与权限：`app/utils/session_auth.py`

- `login_user()` 把 `user_id/username/role` 放到 `session`。
- `load_current_user()` 每次请求从 session 读用户并挂载到 `g.user`。
- `login_required()`：
  - 未登录访问 API 返回 `401` JSON；
  - 未登录访问页面会重定向到登录页；
  - 角色不匹配返回 `403` 或跳转。

### 5.3 路由分层

- 页面路由：`app/routes/site.py`
  - 负责返回 HTML 模板。
- 业务 API：`app/routes/api.py`
  - 负责校验参数、读写数据库、读写 OSS、返回 JSON。
- 账户 API：`app/routes/account.py`
  - 用户列表、当前用户、简介更新。

## 6. 记录系统的后端实现（最关键流程）

### 6.1 发布记录（文本或文件）

入口：
- `POST /api/uploads/prepare`（可选：申请直传 OSS 签名与 upload token）
- `POST /api/push`（别名）
- `POST /api/records`（标准接口）

核心函数：`app/routes/api.py::_create_record_for_user`

流程：
1. 读取 JSON/Form 参数。
2. 解析标签（`_parse_tags`）与可见性（`_normalize_visibility`）。
3. 判断是否上传文件：
   - 直传成功：前端先请求 `/api/uploads/prepare`，拿到签名 URL 上传后，提交 `uploaded_file_token` 落库。
   - 直传不可用或失败：回退 `_file_to_content`，先保存到临时目录 `data/uploads/`，计算 `sha256`，上传到 OSS（或本地对象目录），生成 `Content(kind=file)`。
   - 无文件：要求 `text` 非空，生成 `Content(kind=text)`。
4. 创建 `Record` 并关联 `Content`，写入 `preview/tags/visibility`。
5. `db.session.commit()`。
6. 返回记录摘要 JSON。

文件类型判定规则（`_detect_file_type`）：
- `kind=text` 直接记为 `text`。
- 文件记录按 `content_type + 扩展名` 推断为：
  - `web`（HTML/XHTML）
  - `image/video/audio/text`
  - `log`（如 `.log/.out/.err`）
  - `database`（如 `.db/.sqlite/.sqlite3`）
  - `archive`（如 `.zip/.tar/.gz/.7z/.rar`）
  - `document`（如 `.pdf/.docx/.pptx/.xlsx/.odt`）
  - 兜底 `file`
- 推断结果写入数据库 `Content.file_type`（以及 `GeneratedAsset.file_type`），读取时会做复查。

### 6.2 读取记录列表

入口：`GET /api/pull`、`GET /api/records`、`GET /api/echoes`

关键点：
- 可见性过滤在 `_visible_filter`：
  - 默认“公开 + 自己私密”；
  - `public_only=1` 时仅公开。
- 支持按 `user_id/tag/day` 过滤（`_apply_filter_values`）。
- 返回结构由 `_record_payload` 统一组装。
- 列表接口默认使用 `content_source=summary`：文本内容优先走 DB 里的概述字段，不在 feed 阶段回源 OSS。
- 详情接口（`GET /api/records/<id>`）默认使用 `content_source=full`：按需读取对象存储中的完整文本/文件。
- 若确需签名直链，可显式传 `include_signed_url=1`（并可用 `signed_url_expires` 控制时效，默认短时）。

### 6.3 更新记录

入口：`PATCH /api/records/<id>`

规则：
- 只有作者可改（`record.user_id == current_user.id`）。
- 可改 `visibility/tags/text/file`。
- 若替换文件，会删除旧对象存储文件（尽力删除，失败不阻塞主流程）。

### 6.4 删除记录

入口：`DELETE /api/records/<id>`

动作：
1. 校验作者权限。
2. 删除 `Record` 和关联 `Content`。
3. 若是文件记录，尝试删除 OSS 对象。

### 6.5 评论

- `GET /api/records/<id>/comments`：列出评论。
- `POST /api/records/<id>/comments`：发布评论。

规则：
- 必须“能看到该记录”才可评论；
- 评论长度限制 `<=2000`。

### 6.6 媒体区分机制（业务层）

这是最容易混淆的点，当前实现分三层：

1. 存储层（DB）：
   - `Content.kind` 只有 `text | file`，表示“存储形态”；
   - `Content.file_type` 保存统一文件类型（`text/web/image/video/audio/log/database/archive/document/file`）。
2. 资产层（GeneratedAsset）：
   - `GeneratedAsset.file_type` 保存资产类型（同一套枚举）。
3. 渲染/筛选层（Echoes）：
   - 使用统一 `file_type` 规则做筛选和徽标展示；读取时会按元数据复查类型。

代码对应：
- 文件类型推断（复用入口）：`app/routes/api.py::_detect_file_type`
- 通用媒体推断（渲染用）：`app/routes/api.py::_content_media_type`
- Echoes 记录/资产分类：`app/routes/api.py::_record_echo_file_type`、`_asset_echo_file_type`
- Echoes 后端筛选：`app/routes/api.py::_record_file_type_filter`、`_asset_file_type_filter`
- Echoes 前端标签映射：`app/static/js/site.js` 中 `echoFileTypeLabels`

## 7. Board / Echoes / Notice 后端逻辑

### 7.1 Board（统计矩阵）

入口：
- `GET /api/board`：返回日期列表、用户列表、统计矩阵 `matrix`。
- `GET /api/board/cell`：某用户某天记录。
- `GET /api/board/user/<id>/records`：某用户可见记录。
- `GET /api/board/date/<day>`：某天公开记录。

实现点：
- 用 SQL 聚合 `count(record.id)`；
- 前端再渲染成热力表（`app/static/js/site.js`）。

### 7.2 Echoes（公开流）

入口：`GET /api/echoes`

实现点：
- 查公开记录 + 公开 AI 资产；
- 支持分页；
- 支持按 `file_type` 过滤（`text/web/image/video/audio/log/database/archive/document/file`）；
- 返回内容 payload，可直接渲染图片/音频/视频/文件链接；
- 公开博客网页（HTML）归类为 `web`，并以链接方式展示。

### 7.3 Notice（整页拼接）

入口：
- `GET /api/notice/render`：按筛选直接拼 HTML。
- `GET /api/generated-assets`：查询资产列表（支持 `day/kind/visibility/daily_digest`）。
- `GET /api/generated-assets/<id>/blob`：读取生成资产文件（`public` 资产对登录用户可读）。
- `POST /api/digest/daily`：管理员触发“某天公开内容”的日报生成。

实现点：
- `_render_notice_html`：把记录流按日期分组渲染为单页 HTML。
- `_ai_provider_settings`：读取 `AI_CHAT_PROVIDER` 并解析聊天模型配置。
- `_ai_chat`：调用 `/chat/completions`（博客/播客脚本/海报提示词都依赖这一步）。
- `_records_for_ai_prompt`：构建 AI 上下文，优先注入记录全文；文本文件会尝试读取并提取正文（受长度/字节预算限制）。
- `save_daily_archive`：将当天记录完整写入本地 JSON 归档（含文本全文、文件元信息、可提取文本）；该归档落在 `LOCAL_DAILY_ARCHIVE_DIR`，不是数据库表。
- `_capability_settings_candidates`：按“能力 provider -> 备用 provider”选择候选模型。
- `_generate_podcast_asset`：先生成多风格播客脚本，再调用 TTS；外部 TTS 全失败时可本机 `say` 兜底（`audio/aiff`）。
- `_ai_generate_poster_image`：调用 `/images/generations`；外部图像接口全失败时可本地 SVG 兜底。
- `_save_generated_asset`：将 AI 结果写入对象存储 + `GeneratedAsset` 表。
- `build_daily_public_digest`：按日汇总公开内容，产出博客/播客/海报并归档。

## 8. 对象存储设计（OSS 与本地兜底）

代码：`app/oss.py`, `app/utils/oss_paths.py`

统一接口：
- `put_object_from_file`
- `put_object_bytes`
- `get_object_bytes`
- `delete_object`
- `sign_get_url`

行为：
- 配置了远端 OSS 参数时，走 `oss2` 直连 bucket。
- 未配置时，自动落到 `data/oss-local/`，开发机可直接跑通。

命名规则（key）：
- 记录文件：`{prefix}/records/{YYYY-MM-DD}/objects/{uuid}.{ext}`
- 生成资产：`{prefix}/generated/{YYYY-MM-DD}/user-{id}/{kind}/{uuid}.{ext}`

## 9. API 返回结构（你会经常看到的字段）

`Record` payload 典型结构：

```json
{
  "id": 12,
  "record_no": 12,
  "visibility": "public",
  "tags": ["math", "algebra"],
  "preview": "今天完成了线性代数习题...",
  "created_at": "2026-02-16T01:23:45.000000Z",
  "updated_at": "2026-02-16T01:23:45.000000Z",
  "can_edit": true,
  "can_comment": true,
  "user": { "id": 2, "username": "alice" },
  "content": {
    "id": 33,
    "kind": "text",
    "file_type": "text",
    "text": "完整文本..."
  }
}
```

文件内容 `content` 额外字段：
- `file_type`（`text/web/image/video/audio/log/database/archive/document/file`）
- `filename`
- `content_type`
- `size_bytes`
- `sha256`
- `media_type`（`image/video/audio/text/file`）
- `blob_url`（通过后端鉴权下载）
- `signed_url`（默认空；仅在显式请求 `include_signed_url=1` 时返回短时签名直链）

Echoes 的 `entries[]` 额外字段：
- `entry_type`：`record | asset`
- `file_type`：`text/web/image/video/audio/log/database/archive/document/file`（用于 Echoes 分类筛选与徽标）

## 10. 前端如何连接后端（页面到代码文件）

- 页面模板：`app/templates/*.html`
- 统一前端脚本：`app/static/js/site.js`

对应关系：
- Home 页面：
  - 拉取今日公开记录：`GET /api/home/today`
  - 快速发布：`POST /api/push`
  - 查看系统自动生成的上一日博客/播客/海报
  - `today_assets` 仅包含：`visibility=public` 且 `is_daily_digest=true` 且 `source_day=已闭合日` 的资产
  - 手动测试生成的 `private` 资产不会出现在 Home 卡片中
  - 本地向量问答：`POST /api/vector/chat`
  - 重建向量索引：`POST /api/vector/rebuild`
- Board 页面：
  - 主表：`GET /api/board`
  - 点击行/列/单元格触发对应详情 API
- Echoes 页面：
  - `GET /api/echoes`（记录 + 公开资产）
- Notice 页面：
  - `GET /api/users` 填充用户筛选；
  - `GET /api/notice/render` 直接渲染。

### 10.1 前端 CSS 属性与组件总结（输入框/勾选框等）

核心样式文件：`app/static/css/main.css`

- 全局表单控件（输入框/下拉框/文本域）：
  - `input, textarea, select` 统一使用 `border + border-radius + padding + background`，保证跨页面一致性。
  - `textarea` 默认 `resize: vertical`，避免横向拖拽破坏布局。
- 按钮系统：
  - 基础按钮：主色背景，圆角，hover 亮度变化。
  - 次级按钮：`.secondary`，用于低频或次要操作（如管理页手动重建索引）。
- 勾选与开关：
  - 传统勾选：`.inline-check`（保留兼容）。
  - 新统一开关：`.inline-switch` / `.inline-switch-sm` / `.inline-switch-ui` / `.inline-switch-knob` / `.inline-switch-copy`。
  - 已用于登录页“记住登录状态”、管理页“仅看前端覆盖”、管理页布尔配置项。
- Home 页专用交互控件：
  - 可见性分段选择：`.quick-visibility-switch`（radio + pill）。
  - “AI 增强回答”开关：`.vector-ai-toggle`（checkbox + switch UI）。
- 反馈与状态：
  - 统一状态条：`.feedback-inline`（`info/success/warning/error` 色阶）。
  - 按钮忙碌态：`.is-busy` + 旋转指示器。
- 移动端与防溢出策略（重点）：
  - 在容器链路上大量使用 `min-width: 0`，避免 flex/grid 子项撑开页面。
  - 文本容器统一 `overflow-wrap: anywhere` + `word-break: break-word`。
  - 需要保留横向浏览的区域（如 Board 表格）使用局部 `overflow-x: auto`，而不是让页面整体横向滚动。
  - 管理页补充窄屏规则：filter 输入可收缩、分组网格在小屏自动单列、长 key 可换行。

### 10.2 前端 JavaScript 实现总结（你提到的“java 实现”按 JavaScript 汇总）

核心脚本：
- 页面交互主脚本：`app/static/js/site.js`
- 管理台脚本：`app/static/js/admin.js`

`site.js` 结构（按页面分发）：
- 启动入口 `boot()` 基于 `body[data-page]` 路由到 `initHome/initBoard/initEchoes/initNotice`。
- 公共能力：
  - 统一请求封装 `api()`；
  - 反馈展示 `setFeedback()`；
  - 记录弹窗、图片预览、全局操作绑定。
- Home：
  - 快速发布（文本/多文件队列）。
  - 文件优先尝试直传 OSS：`/api/uploads/prepare` + signed URL；失败自动回退普通上传。
  - 标签自动补全：仅在“提交边界”触发（空格/标点/换行/粘贴），并规避中文输入法 composition 中间态。
  - 检索问答：`/api/vector/chat`，AI 回答开关由前端 toggle 控制。
- Board：
  - 读取 `/api/board` 并渲染热力矩阵；
  - 天数配置已从页面输入移到管理台运行时配置。
- Echoes：
  - 按范围/类型筛选；
  - IntersectionObserver 无限滚动加载；
  - 空状态、加载态、结束态统一反馈。
- Notice：
  - 拉取用户筛选并请求 `/api/notice/render`；
  - 阅读工具栏（字号、宽度、字体、媒体折叠、翻译、回顶）统一状态控制。

`admin.js` 结构：
- 拉取配置：`GET /api/admin/settings`，按分组动态渲染字段（string/int/bool/choice/text）。
- 保存配置：`PUT /api/admin/settings`（支持“回退默认”批量提交）。
- 管理体验：
  - 搜索 key/名称/描述；
  - “仅看前端覆盖”过滤；
  - 布尔项统一开关式控件；
  - 手动向量重建入口（低频兜底）。

### 10.3 设计思考（UI 与交互）

1. 一致性优先：输入、开关、按钮、反馈都做统一组件化命名，降低页面间风格跳变。  
2. 低频参数下沉：`VECTOR_TOP_K`、`BOARD_DEFAULT_DAYS` 移到管理台，Home/Board 保持任务导向。  
3. 移动端优先防溢出：优先保证“不炸版”，通过 `min-width: 0`、断词、局部滚动容器组合解决。  
4. 渐进增强：直传 OSS、AI 能力均做失败回退，优先保证发布和阅读主流程可用。  
5. 对输入法友好：标签自动识别避开 composition 阶段，减少中文输入过程误识别。  

## 11. 本地运行（开发）

### 11.1 最小步骤

推荐直接用脚本自动化初始化（包含 venv、依赖、`.env`、数据库、本地归档目录与向量库目录）：

```bash
./benoss.sh bootstrap
./benoss.sh start
# 安装每天 00:00 自动生成日报资产（博客/播客/海报）
./benoss.sh digest-cron install --time 00:00
# 查看/移除该定时任务
./benoss.sh digest-cron status
./benoss.sh digest-cron remove
```

注意：
- `./benoss.sh start` 和 `./benoss.sh bootstrap` 不会自动安装 cron。
- 如果你希望“每天 00:00 自动生成”，必须手动执行一次 `./benoss.sh digest-cron install --time 00:00`。
- 可用 `./benoss.sh digest-cron status` 确认是否已生效。

如需关闭某些自动步骤，可用环境变量：

```bash
BENOSS_AUTO_INIT_DB=0 ./benoss.sh init
```

手动步骤（等价流程）：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
python -m flask --app app init-db
python -m flask --app app run --debug --port 80
# 手动触发日报（默认按 DIGEST_TIMEZONE 跑昨天）
python -m flask --app app digest-build
# 手动重建本地向量索引
python -m flask --app app vector-build
# 强制全量重建（忽略增量缓存）
python -m flask --app app vector-build --force
```

提示：
- 若 `python -m flask --app app digest-build` 报 `Unable to build URLs outside an active request`，可改用管理员接口触发：

```bash
curl -X POST "http://127.0.0.1:80/api/digest/daily" \
  -H "Content-Type: application/json" \
  -b "session=<admin-session-cookie>" \
  -d '{"day":"2026-02-16","timezone":"Asia/Shanghai","force":true}'
```

打开浏览器访问：
- `http://127.0.0.1:80/login`

### 11.2 管理账号

在 `.env` 中配置：

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-me
```

应用启动时会自动确保该账号存在且角色为 `admin`。

### 11.3 文件上传限制

- `MAX_CONTENT_LENGTH` 默认 `1073741824`（1GB）。

## 12. `benoss-sync` 命令行（对接外部目录）

入口文件：`app/cli/sync.py`

安装后可用命令：

```bash
benoss-sync init --base-url http://127.0.0.1:80 --default-tag math
benoss-sync status --username alice
benoss-sync push --username alice --text "today progress" --visibility public
benoss-sync pull --username alice --output ./pulled_records
benoss-sync pull --username alice --content-source full --output ./pulled_records_full
```

说明：
- 会在当前目录写入 `.benoss/config.json` 保存 base_url 和默认 tag。
- `pull` 会把每条记录写成独立目录（含 `record.json` 与内容文件）。
- `pull --content-source summary|full`：
  - `summary`（默认）只拉 DB 概述文本；
  - `full` 按需拉对象存储中的完整文本。

## 13. 环境变量说明

配置定义位置：`app/config.py`，示例文件：`.env.example`。

### 13.1 基础配置

- `SECRET_KEY`
- `DATABASE_URL`（默认 SQLite）
- `REMEMBER_DAYS`
- `SESSION_COOKIE_SAMESITE`
- `SESSION_COOKIE_SECURE`
- `BOARD_DEFAULT_DAYS`

### 13.2 OSS 配置

- `ALIYUN_OSS_ENDPOINT`
- `ALIYUN_OSS_ACCESS_KEY_ID`
- `ALIYUN_OSS_ACCESS_KEY_SECRET`
- `ALIYUN_OSS_BUCKET`
- `ALIYUN_OSS_PREFIX`
- `ALIYUN_OSS_PUBLIC_BASE_URL`
- `ALIYUN_OSS_ASSUME_PUBLIC`
- `OSS_DIRECT_UPLOAD_ENABLED`（前端直传 OSS 开关，默认开启）
- `OSS_DIRECT_UPLOAD_EXPIRES_SECONDS`（直传签名 URL 过期秒数）
- `OSS_DIRECT_UPLOAD_TOKEN_MAX_AGE_SECONDS`（直传落库 token 过期秒数）
- `OSS_REMOTE_FAILOVER_LOCAL`（远端 OSS 写入失败时自动落地本地目录）

提示：若启用前端直传 OSS，需在 OSS Bucket CORS 中允许站点来源发起 `PUT/OPTIONS`。

### 13.3 AI 配置

- `AI_CHAT_PROVIDER`：聊天能力 provider（内置 `openai` / `chatanywhere` / `deepseek` / `aliyun`，也支持自定义前缀 provider）
- `AI_EMBEDDING_PROVIDER`：向量 embedding provider（内置 `openai` / `chatanywhere` / `deepseek` / `aliyun`，也支持自定义前缀 provider）
- `AI_TTS_PROVIDER`：TTS provider（留空表示不调用外部 TTS）
- `AI_IMAGE_PROVIDER`：图片 provider（留空表示不调用外部图像模型）
- `AI_REQUEST_TIMEOUT_SECONDS`
- `AI_MAX_NOTICE_RECORDS`
- `AI_NOTICE_CONTEXT_MAX_CHARS`
- `AI_NOTICE_RECORD_MAX_CHARS`
- `AI_NOTICE_FILE_READ_MAX_BYTES`
- `AI_NOTICE_ATTACH_IMAGES`
- `AI_NOTICE_MAX_IMAGE_ATTACHMENTS`
- `AI_NOTICE_IMAGE_URL_EXPIRES_SECONDS`
- `AI_ARCHIVE_MULTIMODAL_PARSE`（归档阶段非文本文件多模态解析开关）
- `AI_ARCHIVE_PARSE_MAX_CHARS`（多模态解析文本上限）
- `AI_ARCHIVE_PARSE_TIMEOUT_SECONDS`
- `AI_TTS_VOICE`
- `AI_TTS_RESPONSE_FORMAT`
- `AI_TTS_MAX_INPUT_CHARS`
- `AI_TTS_FALLBACK_LOCAL`（外部 TTS 失败时，尝试本机 `say` 兜底）
- `AI_IMAGE_FALLBACK_LOCAL`（图片模型不可用时本地 SVG 兜底）
- `PODCAST_DEFAULT_STYLE`（`dialogue/speech/interview/news`）
- `LOCAL_DAILY_ARCHIVE_DIR`
- `LOCAL_VECTOR_STORE_DIR`
- `VECTOR_AUTO_REBUILD`
- `VECTOR_TOP_K`
- `VECTOR_MAX_DOCS`
- `VECTOR_EMBEDDING_BATCH_SIZE`
- `VECTOR_EMBEDDING_MAX_INPUT_CHARS`
- `DIGEST_TIMEZONE`（默认 `Asia/Shanghai`，用于“每日结束”切分日期）
- `ARCHIVE_RETENTION_DAYS`（默认 `7`，`0` 表示永久保留）
- `ARCHIVE_STORE_FILE_BLOB`（是否在本地归档保存文件本体副本）

按 provider 分组（聊天/向量/TTS/图片/转写）：
- `OPENAI_API_KEY` / `OPENAI_API_BASE_URL` / `OPENAI_CHAT_MODEL` / `OPENAI_EMBEDDING_MODEL` / `OPENAI_TTS_MODEL` / `OPENAI_IMAGE_MODEL` / `OPENAI_TRANSCRIBE_MODEL`
- `CHAT_ANYWHERE_API_KEY` / `CHAT_ANYWHERE_API_BASE_URL` / `CHAT_ANYWHERE_CHAT_MODEL` / `CHAT_ANYWHERE_EMBEDDING_MODEL` / `CHAT_ANYWHERE_TTS_MODEL` / `CHAT_ANYWHERE_IMAGE_MODEL` / `CHAT_ANYWHERE_TRANSCRIBE_MODEL`
- `DEEPSEEK_API_KEY` / `DEEPSEEK_API_BASE_URL` / `DEEPSEEK_CHAT_MODEL` / `DEEPSEEK_EMBEDDING_MODEL` / `DEEPSEEK_TTS_MODEL` / `DEEPSEEK_IMAGE_MODEL` / `DEEPSEEK_TRANSCRIBE_MODEL`
- `ALIYUN_AI_API_KEY` / `ALIYUN_AI_API_BASE_URL` / `ALIYUN_AI_CHAT_MODEL` / `ALIYUN_AI_EMBEDDING_MODEL` / `ALIYUN_AI_TTS_MODEL` / `ALIYUN_AI_IMAGE_MODEL` / `ALIYUN_AI_TRANSCRIBE_MODEL`
- 自定义 provider 也支持同样命名规则：`<PROVIDER>_API_KEY` / `<PROVIDER>_API_BASE_URL` / `<PROVIDER>_CHAT_MODEL` / `<PROVIDER>_EMBEDDING_MODEL` / `<PROVIDER>_TTS_MODEL` / `<PROVIDER>_IMAGE_MODEL` / `<PROVIDER>_TRANSCRIBE_MODEL`（例如 `GROQ_*`）

说明：
- 媒体与向量模型现在都是“每个 provider 独立配置”，不再使用全局 `AI_TTS_MODEL` / `AI_IMAGE_MODEL` / `VECTOR_EMBEDDING_MODEL`。
- `unsupported/none` 等占位值会被视为“该 provider 该能力禁用”。

能力路由规则（当前版本）：
- 聊天（博客正文/播客脚本/海报提示词）只走 `AI_CHAT_PROVIDER`。
- 向量 embedding 只走 `AI_EMBEDDING_PROVIDER`（读取该 provider 的 `*_EMBEDDING_MODEL`）。
- TTS / 图片先走 `AI_TTS_PROVIDER` / `AI_IMAGE_PROVIDER`，失败再尝试备用 provider。
- 若 `AI_TTS_FALLBACK_LOCAL=1`，外部 TTS 全失败时自动降级到本机 `say`（产物通常为 `.aiff`）。
- 若 `AI_IMAGE_FALLBACK_LOCAL=1`，外部图像全失败时自动降级到本地 SVG 海报。

### 13.4 能力分流执行顺序（按代码真实行为）

- 聊天（博客正文/播客脚本/海报提示词）：
  - 只读取 `AI_CHAT_PROVIDER` 对应的聊天模型（`*_CHAT_MODEL`），不做 provider 级自动切换。
- 向量 embedding：
  - 只读取 `AI_EMBEDDING_PROVIDER` 对应 provider 的 `*_EMBEDDING_MODEL`，不做 provider 级自动切换。
- TTS / 图片：
  - provider 候选顺序：`AI_TTS_PROVIDER`/`AI_IMAGE_PROVIDER`（必填才会调用外部） -> `openai` -> `chatanywhere` -> `aliyun` -> `deepseek`（去重后依次尝试）。
  - model 选择顺序：每个 provider 只读取自己对应的 `*_TTS_MODEL` / `*_IMAGE_MODEL`。
  - 若模型值是 `unsupported/none` 等占位符，该 provider 会跳过该能力调用。
  - 全部外部 provider 失败后，若开启本地兜底：
    - `AI_TTS_FALLBACK_LOCAL=1` -> `macos-say`（`.aiff`）
    - `AI_IMAGE_FALLBACK_LOCAL=1` -> 本地 SVG

### 13.5 Embedding 404（`model_not_found`）快速排查

典型报错：
- `embedding request failed (404) ... code=model_not_found`

排查步骤：

```bash
# 1) 先看“实际生效”的 embedding provider 与对应 model（含管理员后台覆盖）
python - <<'PY'
from app import create_app
from app.utils.runtime_settings import get_setting_str

app = create_app()
with app.app_context():
    provider = get_setting_str("AI_EMBEDDING_PROVIDER").strip()
    mapping = {
        "openai": "OPENAI_EMBEDDING_MODEL",
        "chatanywhere": "CHAT_ANYWHERE_EMBEDDING_MODEL",
        "deepseek": "DEEPSEEK_EMBEDDING_MODEL",
        "aliyun": "ALIYUN_AI_EMBEDDING_MODEL",
    }
    key = mapping.get(provider, "")
    print("AI_EMBEDDING_PROVIDER=", provider)
    if key:
        print(f"{key}=", get_setting_str(key))
PY

# 2) 修正为该 provider 支持的 embedding 模型（示例）
# openai/chatanywhere -> text-embedding-3-small
# aliyun             -> text-embedding-v3
# deepseek           -> 当前无 embedding，需换 provider

# 3) 强制重建索引验证
python -m flask --app app vector-build --force
```

结论：
- 向量链路报 404 时，根因通常不是“本地模型没跑起来”，而是“当前 provider 的 `*_EMBEDDING_MODEL` 与 provider 能力不匹配”。

### 13.6 推荐配置模板（当前版本）

模板 A（全云端，一套 provider 跑全链路，推荐）：

```env
AI_CHAT_PROVIDER=openai
AI_EMBEDDING_PROVIDER=openai
AI_TTS_PROVIDER=openai
AI_IMAGE_PROVIDER=openai
OPENAI_CHAT_MODEL=gpt-4o-mini
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
OPENAI_TTS_MODEL=gpt-4o-mini-tts
OPENAI_IMAGE_MODEL=gpt-image-1
AI_TTS_FALLBACK_LOCAL=0
AI_IMAGE_FALLBACK_LOCAL=0
```

模板 B（主用阿里云，媒体走本地兜底）：

```env
AI_CHAT_PROVIDER=aliyun
AI_EMBEDDING_PROVIDER=aliyun
AI_TTS_PROVIDER=aliyun
AI_IMAGE_PROVIDER=aliyun
ALIYUN_AI_CHAT_MODEL=qwen-plus
ALIYUN_AI_EMBEDDING_MODEL=text-embedding-v3
ALIYUN_AI_TTS_MODEL=qwen3-tts-instruct-flash
ALIYUN_AI_IMAGE_MODEL=qwen-image-max
AI_TTS_FALLBACK_LOCAL=1
AI_IMAGE_FALLBACK_LOCAL=1
```

说明：
- 若你希望严格固定某个能力只走单一 provider，请只配置该 provider 的 key/base_url/model，其他 provider 相关 key 留空。

## 14. 面向未来的扩展方向

这个项目现在已经能稳定承载“记录流 + 资产生成”的核心闭环，后续可以沿这些方向自然演进：

1. 数据层可演进为显式迁移（如 Alembic），让生产升级更可控。
2. 标签可从 `tags_json` 升级为独立标签表 + 关联表，提升复杂检索能力。
3. AI 任务可异步化（队列 + worker），避免长请求占用 Web 线程。
4. 可补充对象存储垃圾回收任务，定期校验“数据库记录 vs 实际文件”。
5. 可增加审计日志和速率限制，强化多用户环境下的可观测性与安全性。
6. 向量检索后端可从当前本地 `data/vector-store/index.json` 演进为专用向量数据库（如 `pgvector` / Milvus / OpenSearch 向量能力），并保留现有 `content_hash` 增量 upsert 语义与 `citations` 返回结构，降低迁移对前端和 API 的影响。
7. OSS 可用于向量索引快照备份与分发，但不作为在线向量检索引擎；在线检索仍应由支持 ANN/过滤检索的向量数据库承担。
