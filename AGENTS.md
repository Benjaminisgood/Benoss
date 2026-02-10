## Benoss Agent Notes (Repo Instructions)

这个仓库是一个 Flask 应用，目标是做一个“好朋友一起成长和学习”的学习项目平台：
- Blog / Note = Projects（类似 repo）
- 文件任意类型（图片/音频/视频/文本/代码/文档）
- clone（复制公开项目到自己的私有项目）
- push（owner 直接上传/删除文件）
- propose push（非 owner 发起 push request，owner 在 Control Room 同意后合入）
- Echoes（公开项目文件流）
- Home（全员可编辑 Quick Links + 公共白板）
- Dailyreel（日历 + 当日榜单与 feed）

下面是给后续维护/扩展的工程指引（尤其是“git 逻辑”的实现约束）。

### 1) 关键模块与入口
- Flask app：`app/__init__.py`
- 路由注册：`app/routes/__init__.py`
- 核心 API：
  - Projects：`app/routes/projects.py`
  - Dailyreel：`app/routes/dailyreel.py`
  - Whiteboard：`app/routes/whiteboard.py`
  - Quick Links：`app/routes/links.py`
  - Account：`app/routes/account.py`
  - Admin Users：`app/routes/admin.py`
- 认证/鉴权（session）：`app/utils/session_auth.py`
- OSS 访问：`app/oss.py` + key 规则：`app/utils/oss_paths.py`
- 前端 JS：
  - 站点/页面交互：`app/static/js/site.js`
  - Control Room（账号/协作/用户管理）：`app/static/js/admin.js`
- CLI 增量同步：`app/cli/sync.py`（命令 `benoss-sync`）

### 1.5) 数据存储方式（总览）
Benoss 把“业务状态”和“文件内容”分开存：
- SQLite（Flask-SQLAlchemy）：存业务状态与元数据（用户、项目、文件索引、活动、push request、白板等）
  - 默认 `DATABASE_URL=sqlite:///data/benoss.sqlite`（见 `app/config.py`）
- OSS（Aliyun OSS）：存实际文件 bytes；DB 只保存 `oss_key` 指针 + `sha256/size/content_type` 等
- 本地临时目录：上传会先落盘到 `UPLOAD_TMP_DIR`（默认 `data/uploads/`），计算 hash 后再 put 到 OSS，最后删除临时文件（见 `app/routes/projects.py` / `app/routes/whiteboard.py`）

OSS 前缀布局（`OSS_PREFIX` 默认 `benoss`）：
```text
{OSS_PREFIX}/projects/{project_uuid}/
  objects/{file_uuid}{ext}                 # 项目正式文件
  push/{push_request_id}/{file_uuid}{ext}  # push request 暂存文件

{OSS_PREFIX}/whiteboard/{YYYY-MM-DD}/
  objects/{file_uuid}{ext}                 # 白板附件/媒体
```

### 2) 数据模型（“git 核心”）
主要表定义在 `app/models.py`：
- `Project`：一个项目=一个“repo”
  - `uuid` 用于 OSS key 前缀（避免暴露自增 id，且便于分桶）
  - `module` 固定为 `blog` 或 `note`（只是分类，不影响内容类型）
  - `visibility`: `public|private`
  - `owner_id`: owner 才能 edit/push
- `ProjectFile`：一个项目内的“追踪文件”（按 `path` 唯一）
  - `path` 是 POSIX 相对路径（例如 `assets/img.png`）
  - `oss_key` 是随机对象 key（真实内容在 OSS）
  - `sha256`/`size_bytes`/`content_type` 用于增量判断与预览
- `ProjectActivity`：记录 git/clone/push/push_request/approve 等活动（Dailyreel 统计使用）
- `PushRequest` + `PushRequestFile`：协作提交（类似 PR 的最小实现）
- `QuickLink`：Home 的 Quick Links（全员可改）
- `WhiteboardCard` + `WhiteboardEvent`：公共白板（事件轮询）

### 3) “Git 行为”如何映射到系统里
这里的“git”是产品语义，不是完整 git 协议：

- git（创建 repo）
  - API：`POST /api/projects`
  - DB：插入 `Project` + 写入 `ProjectActivity(type='git')`

- push（上传/覆盖/删除文件）
  - API：
    - 上传：`POST /api/projects/<id>/files/upload`（multipart）
    - 删除：`DELETE /api/projects/<id>/files`（json `{path}`）
  - 语义：
    - `path` 相同则覆盖：更新 `ProjectFile.oss_key`（旧对象 best-effort 删除）
    - `path` 不存在则新增：插入 `ProjectFile`
  - 安全：
    - `path` 走 `_safe_relpath()`，拒绝 `..`、空段、绝对路径等
  - 增量基础：
    - 服务端计算 `sha256` 并写入 DB（`app/routes/projects.py`）

- clone（复制一个项目）
  - API：`POST /api/projects/<id>/clone`
  - 行为：
    - 服务器把源项目每个文件的 OSS 对象 copy 到新项目随机 key
    - 新项目默认 `private`
    - `Project.cloned_from_id` 关联源项目
    - 写 `ProjectActivity(type='clone')`（记录在源项目上）

- propose push（向他人项目提交变更）
  - API：
    1) `POST /api/projects/<id>/push-requests` 创建请求（pending）
    2) `POST /api/push-requests/<pr_id>/files/upload` 上传候选文件（存到 push/ 前缀）
  - 合入：
    - owner `POST /api/push-requests/<pr_id>/approve`
    - 服务器 copy push/ 对象到 objects/ 下新 key，并 upsert `ProjectFile(path=...)`
    - 写入 `ProjectActivity(type='push_approve', initiator_user_id=proposer)`

注意：这里没有“分支/冲突/三方合并”。`path` 相同即视为覆盖，合入即最后写入者为准。

### 4) OSS key 规则（强约束）
代码：`app/utils/oss_paths.py` + `app/routes/projects.py`

一个项目的对象都放在：
```text
{OSS_PREFIX}/projects/{project_uuid}/
```

细分：
- 正式文件：`objects/{file_uuid}{ext}`
- push request 暂存：`push/{push_request_id}/{file_uuid}{ext}`

白板媒体对象放在：
```text
{OSS_PREFIX}/whiteboard/{YYYY-MM-DD}/objects/{file_uuid}{ext}
```

原因：
- `path` 只是“repo 内路径”，不直接作为 OSS key（避免覆盖与注入风险）
- 覆盖同一路径时，只更新 DB 指向的新 `oss_key`，旧对象后台删除（best-effort）

### 5) 权限模型
- 全站需要登录（`login_required()`）
- `Project.visibility == 'public'`：任意登录用户可读
- `private`：仅 owner 可读
- 任何项目写操作：仅 owner（或 push request 流程 owner approve）
- Admin：
  - 只有 `.env` 里 `ADMIN_USERNAME/ADMIN_PASSWORD` 自动种子的那个账号是 admin
  - admin 只用于用户管理（创建用户、停用/启用、重置密码）
  - 不存在“把普通用户升级为 admin”的功能

### 6) benoss-sync（增量远程同步）
实现：`app/cli/sync.py`

它不是 git 协议，而是“按 sha256 做最小差异传输”的同步器：
- `status`：对比本地文件与远端 `ProjectFile.sha256`，计算将要 pull/push 的列表
- `pull`：用远端返回的 `file.url` 下载到本地（原子写入：tmp + replace）
- `push`：调用上传 API 写回远端（owner 才能 push）
- `propose`：对他人项目创建 push request 并上传变更文件（owner 同意后合入）
- `.benoss/config.json`：记录 base_url 与 project_id
- `.benossignore`：glob 排除规则（默认会排除 `.git/`、`.venv/`、`node_modules/` 等）

### 7) 开发命令
本地开发：
```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m flask --app app init-db
python -m flask --app app run --debug --port 80
```

快速自检（建议改动后跑）：
```bash
python -m compileall -q app
python -m flask --app app routes
node --check app/static/js/site.js
node --check app/static/js/admin.js
```

### 8) 修改时的注意事项
- 任何接收 `path` 的地方必须做 traversal 防护（参考 `_safe_relpath()`）
- 不要在 OSS key 上复用用户输入路径
- 不要把私有项目文件暴露到 Echoes（Echoes 只从 public projects 聚合）
- 需要“更像真 git”的能力（commit/历史/冲突合并）时，先明确数据模型再做（不要在现有 `ProjectFile` 上硬拼）
