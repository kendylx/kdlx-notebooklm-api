# CLAUDE.md — kdlx-notebooklm-api (self-hosted NotebookLM bridge)

FastAPI bridge cho plugin `kdlx-ai-kontent` (repo anh em, thư mục `bridge/` = vendored copy của `app.py` này — **sửa ở đây rồi sync sang**, hoặc ngược lại nhưng luôn giữ 2 bản giống nhau).

## Deploy

`COPY app.py` bake code vào image — **git pull không đổi container**. Portainer phải rebuild/update stack. Verify qua `GET /version` (`app_mtime`/`NLM_BUILD_SHA`).

## Contracts đang sống (chi tiết đầy đủ: kdlx-ai-kontent `CLAUDE.md` + `DECISIONS.md`)

- Auth: `POST /auth` stage→validate→commit (không bao giờ ghi storage_state trước khi Google xác thực). `auto` = email-match refresh hoặc next free `pN`; sidecar `email.txt` per profile; `DELETE /profiles/{name}` dọn slot chết. `_storage_path` sanitize traversal-safe.
- `/artifact` nhận `title`+`content` (add_text), không chỉ permalink. kind CSV: `audio,quiz,video,infographic,datatable` + alias `both`/`all`. Download: audio=MP3 / video=MP4 / infographic=PNG binary hoặc `{"url":R2}` khi có process params (trim/watermark/delogo/r2); quiz=JSON; datatable=CSV→`{"table":…}`. Quiz language = 2 kênh: `language=lang` → wire `language_code` (notebooklm-py pin post-0.8.6 — bản release ≤0.8.6 bỏ trống field này cho quiz nên quiz luôn ra English) + directive "Write every question…" trong `instructions`; try/except TypeError fallback cho image cũ.
- `/serp` → 9r `/v1/search`. Bearer `NLM_BRIDGE_TOKEN` mọi endpoint ngoài `/health` `/version`.
- `/research` sync giữ cho client cũ; đường chính là kickoff+poll: `POST /research/start` → `task_id` ngay (deep research 20–40min không bao giờ vừa HTTP timeout — blocking POST luôn trả null phía plugin); `GET /research/status/{task_id}` → `pending{state:queued|running}` / `completed{evidence_pack,asks,profile,mode}` / `failed{error,message}`; task registry `_RESEARCH_TASKS` bounded (TTL 1h sau done, cap 64); worker vẫn `async with _lock, _profile_lock(profile)` suốt run → serialization không đổi; task timeout cap 2700s (sync 600s), worker wait_for = timeout + MAX_URLS*120 + 300.
- Media processing: ffmpeg `delogo` inpaint logo Gemini Notebook góc phải dưới (default `88:94:12:6` %, clamp margin 2px), trước watermark overlay; video `trim_start/end`; `wm_opacity` (10–100, default 100) → `colorchannelmixer=aa=` trên wm stream (cả video lẫn infographic `_process_image`). R2 creds: env NLM_R2_* là default, params `r2_account_id`/`r2_access_key_id`/`r2_secret_access_key`/`r2_bucket`/`r2_public_base`/`r2_prefix` override env (plugin reuse `image_r2_*` khi `notebooklm_r2_from_image` on — secret đi qua HTTP LAN, cùng trust model với bearer token). `/version` trả `r2_env` presence-map (không giá trị) cho settings status check.
- **Lock contract (bắt buộc — sai là giết session)**: `__Secure-1PSIDTS` rotate theo chuỗi tuyến tính phía server — 2 client sống đồng thời trên 1 profile = lineage fork → Google revoke SID im lặng (KHÔNG security email; lỗi "served a signed-out page"). Mọi `NotebookLMClient.from_storage(profile)` phải nằm trong `_profile_lock(profile)`; worker nest thêm `_lock` global (order: `_lock` ngoài, profile trong — status/delete chỉ lấy profile lock nên không deadlock). `/status` + `/auth` validate + `/auth` commit + `DELETE /profiles` đều bounded-acquire (8–20s) trả `busy`/`profile_busy`/`bridge_busy` thay vì fork hoặc chờ hàng phút. `/status` busy → `{"authenticated": null, "busy": true}`.

## Nguyên tắc

- Mọi thay đổi contract → update đoạn trên + vendored copy + `CLAUDE.md`/`CODEBASE_MAP.md`/`DECISIONS.md` của plugin repo (xem section "Nguyên tắc ghi nhớ quyết định kỹ thuật" trong plugin CLAUDE.md).
- Bug fix đáng nhớ: ghi cả cách phát hiện vào commit message ("why", không chỉ "what").
