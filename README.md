# Benoss (Flask + OSS)

A redesigned backend for three modules: blog, note, and everyday. Content lives in Aliyun OSS; SQLite stores users, site links, and attachment index data.

## Structure
- `blog/` and `note/`: Markdown + attachments in OSS (Obsidian-compatible).
- `everyday/`: per-month mapping JSON in OSS and media assets by UUID.
- SQLite: user/role, quick links, friend links, and EverydayAttachmentIndex.

## OSS layout (default)
```
{OSS_PREFIX}/
  blog/
    YYYY-MM-DD-name/
      index.md
      uuid.ext
  note/
    YYYY-MM-DD-name/
      index.md
      uuid.ext
  everyday/
    YYYY/
      MM/
        index.json
        media/uuid.ext
```

## Markdown post layout rules
- Each post is a directory; the post "key" is the directory name (e.g. `YYYY-MM-DD-name`).
- The markdown file must be named `index.md`. Lists and detail reads only look for `**/index.md`.
- Uploads via admin always write to `{module}/{date}-{name}/index.md` and place attachments in the same folder.
- Attachment filenames do not need to be UUIDs if you upload manually, but the markdown references must match the actual filenames/paths.
- Non-`index.md` markdown files (for example `{module}/foo.md`) are not listed or readable by the blog/note endpoints.

## Setup
1. Create a virtualenv and install deps:
```
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```
2. Configure `.env` (OSS + optional ADMIN_USERNAME/ADMIN_PASSWORD).
3. Initialize the database:
```
python -m flask --app app init-db
```
4. Sign in at `/login` using the seeded admin user (from `.env`), then create more users in `/admin`.
5. Run the app:
```
python -m flask --app app run --host 0.0.0.0 --port 5002 --debug
```

## UI routes
- `/` home
- `/blog`, `/note` markdown readers
- `/dailyreel` daily reel entry point (role-based redirect)
- `/dailyreel/view` daily reel viewer (calendar)
- `/dailyreel/manage` daily reel composer (admin)
- `/everyday` asset library
- `/control-room` account and admin tools (login required)
- `/login` sign-in page (site access required)

## API (high level)
- `GET /api/blog` and `GET /api/blog/item?key=...`
- `GET /api/note` and `GET /api/note/item?key=...`
- `GET /api/everyday/day?date=YYYY-MM-DD`
- `POST /api/everyday/text`
- `POST /api/everyday/upload` (multipart form)
- `POST /api/everyday/reel/render`
- `GET /api/album` and `POST /api/album/reindex`
- `POST /api/admin/login` (returns token)
- `GET/POST /api/admin/...` (admin endpoints)

## Reel rendering
The current implementation generates a manifest JSON and stores it in OSS. A full ffmpeg pipeline can be added later; set `REEL_RENDERER=ffmpeg` to wire in a real renderer.

## Direct OSS upload
Everyday manage first attempts a signed PUT upload from the browser to OSS, then falls back to server upload if it fails. Ensure your OSS bucket CORS rules allow `PUT` from your site origin.
