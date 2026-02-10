# Benoss

Benoss 是一个面向“学习与共同进步分享”的平台，它用一套类 Git 的产品语义来组织内容：
- 一个项目 = 一个 repo（Blog / Note 只是分类）
- repo 里可以放任何文件（图片/音频/视频/文本/代码/文档等）
- clone 公开 repo 到自己名下
- 向别人的 repo 发起 push request（对方同意后合入），实现协作

它不是完整的 Git 协议实现，但核心交互尽量贴近 git 的直觉：路径、增量、push/clone、协作审批。

## 快速开始（本地）
1. 安装依赖：
```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

2. 配置 `.env`（参考 `.env.example`）。

关键点：
- 本站默认是“全站需要登录”的。
- 只有自动种子的 admin 能创建其他用户，因此首次启动建议配置：
  - `ADMIN_USERNAME`
  - `ADMIN_PASSWORD`

3. 初始化 DB：
```bash
python -m flask --app app init-db
```

4. 启动：
```bash
python -m flask --app app run --host 0.0.0.0 --port 80 --debug
```

## 核心概念（Git 语义映射）

### Project = repo
- API：`POST /api/projects` 创建项目（`module=blog|note`）
- DB：`Project`（`uuid/module/owner/visibility/title/description`）
- Activity：创建会写入 `ProjectActivity(type='git')`

### ProjectFile = tracked file（按 path 唯一）
- 每个文件有一个“repo 内路径” `path`（POSIX 相对路径，例如 `assets/a.png`）
- 真实内容存 OSS，DB 里存 `oss_key` 指针 + `sha256/size/content_type` 等元信息
- 同一个 `path` 再上传会覆盖（和 git 里“同一路径的新版本”一致）

### push（owner 直接 push）
- 上传：`POST /api/projects/<id>/files/upload`（multipart: `file` + `path`）
- 删除：`DELETE /api/projects/<id>/files`（json: `{path}`）
- 行为：写入 `ProjectActivity(type='push')`

实现方法（关键点）：
- `path` 严格校验（拒绝 `..`、空段、绝对路径等），见 `app/routes/projects.py::_safe_relpath`
- OSS key 不使用用户输入的 `path`（避免覆盖/注入），而是随机 key：
  - 覆盖同一路径时：只更新 DB 指向的新 `oss_key`，旧对象 best-effort 删除
- 服务端计算 `sha256` 并写入 DB（增量同步的基础）

### clone（复制 repo）
- API：`POST /api/projects/<id>/clone`
- 行为：
  - 服务器把源项目每个对象在 OSS 上 copy 到新项目（新 key）
  - 新项目默认 `private`
  - 写入 `ProjectActivity(type='clone')`（记录在“源项目”上）

### push request（类似 PR 的最小实现）
如果你不是 owner，不能直接 push，但可以发起 push request：
1. 创建请求：`POST /api/projects/<id>/push-requests`（返回 `push_request_id`）
2. 上传候选文件：`POST /api/push-requests/<push_request_id>/files/upload`
3. owner 在 Control Room 同意：`POST /api/push-requests/<push_request_id>/approve`

合入实现：
- owner approve 时，服务器把 push request 暂存对象 copy 到正式 `objects/` 下新 key
- 以 `path` 为键 upsert `ProjectFile`（相同路径即覆盖）
- 写入 `ProjectActivity(type='push_approve', initiator_user_id=<proposer>)`

限制：
- 没有分支、没有三方合并、没有冲突解决
- 相同 `path` 的文件以最后一次写入为准（简化版协作）

## 页面与功能
- `/` Home：Quick Links（全员可编辑）+ 公共白板（每天换新、事件轮询同步）
- `/blog` / `/note`：Projects 列表 + repo 窗口（文件列表、预览、上传/删除、clone、propose push）
- `/echoes`：公开项目文件流（聚合 public projects 的文件）
- `/dailyreel`：日历 + 指定日期的榜单与 feed
- `/control-room`：账号简介 + 协作收件箱/发件箱 + 用户管理（仅 seeded admin）

## 存储布局

Benoss 把“业务状态”和“文件内容”分层存储：
- SQLite：存业务状态与元数据（用户、项目、文件索引 `path -> oss_key`、活动、push request、白板等），默认 DB 在 `data/benoss.sqlite`（可通过 `DATABASE_URL` 覆盖）
- OSS（Aliyun OSS）：存实际文件 bytes（项目文件 / push request 暂存 / 白板媒体），DB 只保存 `oss_key` 指针 + `sha256/size/content_type` 等
- 本地临时目录：上传时先落盘到 `data/uploads/`（`UPLOAD_TMP_DIR`），计算 `sha256` 后再上传 OSS，随后删除临时文件

### SQLite（业务状态）
用户、Quick Links、Projects、Files、Activity、Push Requests、Whiteboard 都在 SQLite。

### OSS（文件内容）
默认 key 布局（`OSS_PREFIX` 默认 `benoss`）：
```text
{OSS_PREFIX}/projects/{project_uuid}/
  objects/{file_uuid}{ext}                 # 项目正式文件
  push/{push_request_id}/{file_uuid}{ext}  # push request 暂存文件

{OSS_PREFIX}/whiteboard/{YYYY-MM-DD}/
  board.json                               # 白板 JSON 快照（schema_version=2）
  objects/{file_uuid}{ext}                 # 白板附件/媒体
```

文件访问：
- 默认通过签名 GET URL 访问（`app/oss.py::public_url`）
- 如果你有公共域名/CDN，可设置 `ALIYUN_OSS_PUBLIC_BASE_URL` 并启用 `ALIYUN_OSS_ASSUME_PUBLIC=1` 直接拼接 URL

## Dailyreel 的实现（为什么能“看每天”）
- `GET /api/dailyreel/today?date=YYYY-MM-DD&tz_offset=<minutes>`
  - 返回该日所有用户的 git/clone scoreboard + 该日 feed
- `GET /api/dailyreel/month?month=YYYY-MM&tz_offset=<minutes>`
  - 返回“当前用户”该月每天的统计，用于日历高亮与数字

注意：`tz_offset` 用浏览器的 `Date().getTimezoneOffset()`（分钟），保证“按本地日历日”统计不串天。

## benoss-sync：远程同步（OSS <-> 本地目录）
安装依赖后会提供 `benoss-sync` 命令。它做的是“按 sha256 的增量同步”，不是 git 协议，但体验上更像 `git pull/push`。

### 初始化（绑定 remote）
```bash
benoss-sync init --base-url http://127.0.0.1:80 --project 123 --dir ./myrepo
```
它会写入 `./myrepo/.benoss/config.json`（类似 repo 的 remote 配置）。

### status（查看将要同步什么）
```bash
BENOSS_USERNAME=xxx BENOSS_PASSWORD=yyy benoss-sync status --dir ./myrepo
```

### pull（拉取远端）
```bash
BENOSS_USERNAME=xxx BENOSS_PASSWORD=yyy benoss-sync pull --dir ./myrepo
```
实现方法：
- 读取 `/api/projects/<id>` 返回的文件列表与 `sha256`
- 本地逐文件计算 sha256（如果远端 sha256 缺失则退化为 size 判断）
- 只下载变化文件；写入采用 tmp + `os.replace` 原子替换，避免半写入损坏

### push（推送到远端，仅 owner 可用）
```bash
BENOSS_USERNAME=xxx BENOSS_PASSWORD=yyy benoss-sync push --dir ./myrepo
```
实现方法：
- 对比本地 sha256 与远端 sha256
- 只上传变化文件到 `/api/projects/<id>/files/upload`

### propose（向他人 repo 发起 push request）
```bash
BENOSS_USERNAME=xxx BENOSS_PASSWORD=yyy benoss-sync propose --dir ./myrepo --message "请合入这些更新"
```
实现方法：
- 创建 push request（pending）
- 上传变化文件到 push request（owner approve 后 server-side copy 合入）

### 忽略规则
- 默认忽略：`.benoss/**`、`.git/**`、`.venv/**`、`node_modules/**`、`__pycache__/**` 等
- 可在 repo 根目录创建 `.benossignore`（glob，一行一个）追加忽略

## Admin（只有自动种子的那一个）
- admin 账号由 `.env` 的 `ADMIN_USERNAME/ADMIN_PASSWORD` 自动创建（首次启动）
- admin 只负责“用户管理”（创建用户、停用/启用、重置密码）
- 系统不提供“把普通用户升级为 admin”的功能

## API 概览（常用）
Projects：
- `GET /api/projects?module=blog|note`
- `POST /api/projects`
- `GET /api/projects/<id>`
- `PATCH /api/projects/<id>`
- `DELETE /api/projects/<id>`
- `POST /api/projects/<id>/files/upload`
- `DELETE /api/projects/<id>/files`
- `GET /api/projects/<id>/file/text?path=...`
- `POST /api/projects/<id>/clone`
- `GET /api/projects/public/files`（Echoes，含公共白板媒体）

Collab：
- `POST /api/projects/<id>/push-requests`
- `POST /api/push-requests/<id>/files/upload`
- `POST /api/push-requests/<id>/approve|reject|cancel`
- `GET /api/collab/inbox|outbox`

Home：
- `GET/POST/PATCH/DELETE /api/links/quick...`
- `GET /api/whiteboard/board`、`GET /api/whiteboard/events`
- `POST/PATCH/DELETE /api/whiteboard/cards...`
- `POST /api/whiteboard/links`、`DELETE /api/whiteboard/links/<id>`
- `GET /api/whiteboard/export`、`POST /api/whiteboard/import`
- `GET /api/whiteboard/snapshot`（返回 OSS 的 `board.json` 签名 URL）

Dailyreel：
- `GET /api/dailyreel/today`
- `GET /api/dailyreel/month`

## 环境变量
必需/常用：
- `SECRET_KEY`
- `ADMIN_USERNAME` / `ADMIN_PASSWORD`（建议必配，用于首个 admin）
- `DATABASE_URL`（默认 `sqlite:///data/benoss.sqlite`）
- `REMEMBER_DAYS`（默认 30）

OSS：
- `ALIYUN_OSS_ENDPOINT`
- `ALIYUN_OSS_ACCESS_KEY_ID`
- `ALIYUN_OSS_ACCESS_KEY_SECRET`
- `ALIYUN_OSS_BUCKET`
- `ALIYUN_OSS_PREFIX`（默认 `benoss`）
- `ALIYUN_OSS_PUBLIC_BASE_URL`（可选）
- `ALIYUN_OSS_ASSUME_PUBLIC`（可选，`1` 表示直接拼 public url）

上传/安全：
- `MAX_CONTENT_LENGTH`（默认 1GiB）
- `SESSION_COOKIE_SECURE`（`1` 仅 https）
- `SESSION_COOKIE_SAMESITE`（默认 `Lax`）
