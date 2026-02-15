# Benoss (Tag-Driven)

Benoss 当前是一个以 `Record / Content / Comment` 为核心、按 `Tag` 组织内容的学习记录平台。

## 核心特性

- `push`：发布文本或文件记录（可公开/私密）
- `pull`：拉取当前用户可见记录（本人私密 + 所有公开）
- 文件落 OSS（文本直接入库）
- 评论系统：对可见记录评论，作者可编辑记录
- 页面：`Home / Board / Echoes / Notice`

## 数据模型

- `User`
- `Record`：编号、用户、创建/编辑时间、格式、公私、标签、内容编号
- `Content`：文本内容或 OSS 文件指针
- `Comment`

## 运行

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m flask --app app init-db
python -m flask --app app run --debug --port 80
```

## API

- `POST /api/push` 创建记录
- `GET /api/pull` 拉取记录（支持 `tag/day`）
- `GET /api/records` 筛选记录
- `PATCH /api/records/<id>` 编辑记录
- `POST /api/records/<id>/comments` 评论
- `GET /api/board` Board 统计（可按 tag）
- `GET /api/echoes` 公开瀑布流
- `GET /api/notice/render` 按筛选直接返回整页拼接 HTML

## Notice AI

- `POST /api/notice/ai` 仅用于 `optimize`（返回 HTML）
- `POST /api/notice/assets` 支持 `podcast / poster`，返回真实音频/图片资产
- 默认走 OpenAI 兼容协议，可选 provider：`chatanywhere / deepseek / aliyun`
- 如果未配置有效 key/base/model，接口会返回 `501`

## benoss-sync

```bash
python -m app.cli.sync init --base-url http://127.0.0.1:80 --default-tag math
python -m app.cli.sync status --username alice
python -m app.cli.sync push --username alice --text "today progress" --visibility public
python -m app.cli.sync pull --username alice --output ./pulled_records
```

## OSS 变量

- `ALIYUN_OSS_ENDPOINT`
- `ALIYUN_OSS_ACCESS_KEY_ID`
- `ALIYUN_OSS_ACCESS_KEY_SECRET`
- `ALIYUN_OSS_BUCKET`
- `ALIYUN_OSS_PREFIX`

未配置 OSS 时自动退化到本地对象存储目录：`data/oss-local/`。

## AI 变量

- `AI_AUTOFILL_PROVIDER`：`chatanywhere` / `deepseek` / `aliyun`
- `CHAT_ANYWHERE_API_KEY` / `CHAT_ANYWHERE_API_BASE_URL` / `CHAT_ANYWHERE_MODEL`
- `DEEPSEEK_API_KEY` / `DEEPSEEK_API_BASE_URL` / `DEEPSEEK_MODEL`
- `ALIYUN_AI_API_KEY` / `ALIYUN_AI_API_BASE_URL` / `ALIYUN_AI_MODEL`
