# CLAUDE.md — kdlx-notebooklm-api (self-hosted NotebookLM bridge)

FastAPI bridge cho plugin `kdlx-ai-kontent` (repo anh em, thư mục `bridge/` = vendored copy của `app.py` này — **sửa ở đây rồi sync sang**, hoặc ngược lại nhưng luôn giữ 2 bản giống nhau).

## Deploy

`COPY app.py` bake code vào image — **git pull không đổi container**. Portainer phải rebuild/update stack. Verify qua `GET /version` (`app_mtime`/`NLM_BUILD_SHA`).

## Contracts đang sống (chi tiết đầy đủ: kdlx-ai-kontent `CLAUDE.md` + `DECISIONS.md`)

- Auth: `POST /auth` stage→validate→commit (không bao giờ ghi storage_state trước khi Google xác thực). `auto` = email-match refresh hoặc next free `pN`; sidecar `email.txt` per profile; `DELETE /profiles/{name}` dọn slot chết. `_storage_path` sanitize traversal-safe.
- `/artifact` nhận `title`+`content` (add_text), không chỉ permalink. kind CSV: `audio,quiz,video,infographic,datatable` + alias `both`/`all`. Download: audio=MP3 / video=MP4 / infographic=PNG binary hoặc `{"url":R2}` khi có process params (trim/watermark/delogo/r2); quiz=JSON; datatable=CSV→`{"table":…}`.
- `/serp` → 9r `/v1/search`. Bearer `NLM_BRIDGE_TOKEN` mọi endpoint ngoài `/health` `/version`.
- Media processing: ffmpeg `delogo` inpaint logo Gemini Notebook góc phải dưới (default `88:94:12:6` %, clamp margin 2px), trước watermark overlay; video `trim_start/end`; R2 creds chỉ trong env bridge.

## Nguyên tắc

- Mọi thay đổi contract → update đoạn trên + vendored copy + `CLAUDE.md`/`CODEBASE_MAP.md`/`DECISIONS.md` của plugin repo (xem section "Nguyên tắc ghi nhớ quyết định kỹ thuật" trong plugin CLAUDE.md).
- Bug fix đáng nhớ: ghi cả cách phát hiện vào commit message ("why", không chỉ "what").
