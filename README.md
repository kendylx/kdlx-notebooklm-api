# kdlx-notebooklm-api

HTTP bridge tự host cho **Google NotebookLM**, xây trên [`notebooklm-py`](https://github.com/teng-lin/notebooklm-py) (FastAPI + Docker). Plugin WordPress `kdlx-ai-kontent` gọi bridge này để:

- **Research** — nạp URL nguồn vào notebook, hỏi và trả về `evidence_pack` có cấu trúc (claims, hard numbers, conflicts, coverage matrix, freshness…)
- **Artifacts** — sinh Audio Overview (MP3) và Quiz (JSON) từ Studio của NotebookLM

Không phụ thuộc license/server của bên thứ ba — chỉ cần một tài khoản Google đã đăng nhập NotebookLM.

---

## Kiến trúc

```
WordPress plugin (AIBGResearchClient)
        │  HTTPS, Authorization: Bearer <NLM_BRIDGE_TOKEN>
        ▼
   nlm-bridge (FastAPI, port 8767)
        │  notebooklm-py 0.8.5
        ▼
   NotebookLM (tài khoản Google của bạn)
```

Session Google lưu trong `/data/profiles/<tên>/storage_state.json` — mount từ host (local) hoặc named volume + đẩy qua API (VPS/Portainer). Mỗi "profile" = 1 tài khoản Google riêng, dùng để xoay vòng khi hết quota.

## API

Tất cả endpoint (trừ `/health`) yêu cầu header `Authorization: Bearer <NLM_BRIDGE_TOKEN>`.

| Endpoint | Mô tả |
|---|---|
| `GET /health` | `{"ok": true, "mock": false}` — kiểm tra sống |
| `GET /status?profile=p1` | Kiểm tra session còn hạn — trả `{"authenticated": true, "email": …}` |
| `GET /profiles` | Liệt kê profile đã có `storage_state.json` |
| `POST /auth` | Upload session: Playwright `storage_state`, mảng Cookie-Editor, hoặc chuỗi `Cookie:` header. `{"profile": "auto"}` = tự gán `p1, p2, …` |
| `POST /research` | `{"keyword", "urls": [], "profile", "mode": "fast"|"deep", "timeout"}` → `evidence_pack` |
| `POST /artifact` | `{"kind": "audio"|"quiz"|"both", "urls": [permalink], "profile", "lang", "audio_format": brief/deep_dive/critique/debate, "audio_length": short/default/long, "quiz_quantity": fewer/standard/more, "quiz_difficulty": easy/medium/hard}` → `{"tasks": {"audio": "<task_id>"}}` |
| `GET /artifact/status?task_id=&profile=` | Poll: `pending`/`in_progress`/`completed`/`failed` |
| `GET /artifact/download?task_id=&kind=audio|quiz&profile=` | Audio → file MP3. Quiz → `{"status":"success","quiz":[…]}` |

Artifacts chạy trên notebook tên `ai-seo:<profile>` (tự tạo nếu chưa có), source = các URL bạn truyền vào.

---

## Yêu cầu

- Docker + Docker Compose plugin (VPS), hoặc Portainer
- 1 tài khoản Google đã dùng NotebookLM
- Với VPS: 1 máy local có `pip` để login rồi đẩy session lên (hoặc export cookie từ trình duyệt)

## 1. Chạy local (máy có trình duyệt)

```bash
git clone https://github.com/kendylx/kdlx-notebooklm-api.git
cd kdlx-notebooklm-api

# Đặt token thật — BẮT BUỘC, đừng dùng mặc định
cp .env.example .env && openssl rand -hex 32   # dán kết quả vào NLM_BRIDGE_TOKEN trong .env

# Login Google trên host (session rơi vào ~/.notebooklm — compose bind-mount sẵn)
# Extra [browser] cần cho bước login qua trình duyệt (auto-download Chromium)
uv tool install "notebooklm-py[browser]"   # hoặc: pipx install "notebooklm-py[browser]"
notebooklm -p p1 login          # mở trình duyệt, đăng nhập Google

docker compose up -d --build

# Kiểm tra
curl http://localhost:8767/health
curl -H "Authorization: Bearer <token>" http://localhost:8767/status?profile=p1
```

`compose.yml` bind-mount `$HOME/.notebooklm` vào `/data` → login trên host là container thấy ngay, không cần upload.

## 2. Deploy VPS

```bash
# Trên VPS
git clone https://github.com/kendylx/kdlx-notebooklm-api.git
cd kdlx-notebooklm-api
cp .env.example .env            # sửa NLM_BRIDGE_TOKEN

# File portainer là file ĐỘC LẬP (đã gồm build: . + named volume):
docker compose -f compose.portainer.yml up -d --build
```

Đẩy session từ máy local lên VPS:

```bash
# Trên máy local — đã `notebooklm -p p1 login`
NLM_BRIDGE_URL=http://<ip-vps>:8767 NLM_BRIDGE_TOKEN=<token> ./push-profile.sh p1

# Hoặc upload thẳng cookie export từ trình duyệt (Cookie-Editor / storage_state):
curl -X POST http://<ip-vps>:8767/auth \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"profile":"auto","storage_state":<nội-dung-json>}'
```

**Khuyến nghị**: đặt sau reverse proxy có TLS (Nginx Proxy Manager / Caddy / nginx + certbot) thay vì expose `8767` trần — token đi qua HTTP thường sẽ bị nghe lén. Ví dụ Caddyfile:

```
bridge.domain.com {
    reverse_proxy 127.0.0.1:8767
}
```

## 3. Deploy Portainer

> ⚠️ **Không dùng Web editor** — web editor không có build context nên `build: .` sẽ báo
> *"service has neither an image nor a build context"*. Bắt buộc dùng **Repository** mode.

1. **Stacks → Add stack** → đặt tên `nlm-bridge` → chọn build method **Repository**
2. Điền:
   - **Repository URL**: `https://github.com/kendylx/kdlx-notebooklm-api`
   - **Repository reference**: `refs/heads/main`
   - **Compose path**: `compose.portainer.yml`
   - Repo đang **private** → bật **Authentication**: username GitHub + Personal Access Token scope `repo` ([tạo tại đây](https://github.com/settings/tokens))
3. Thêm **Environment variable**: `NLM_BRIDGE_TOKEN` = `<token thật>`
4. **Deploy the stack** → Portainer clone repo → build image → chạy (named volume `nlm-data` tự tạo)
5. Đẩy session từ máy local bằng `push-profile.sh` hoặc `POST /auth` như mục VPS

**Update sau này**: vào stack → **Pull and redeploy** (kéo commit mới + rebuild), hoặc bật **Re-pull image / GitOps updates**.

## Nhiều tài khoản Google (profile)

```bash
notebooklm -p p2 login                                   # local
./push-profile.sh p2                                     # đẩy lên bridge
curl -H "Authorization: Bearer <t>" http://host:8767/profiles   # → {"profiles":["p1","p2"]}
```

Truyền `"profile": "p2"` trong request `/research`/`/artifact` để chọn tài khoản — hữu ích khi hết quota research của 1 tài khoản.

## Nâng cấp khi `notebooklm-py` phát hành bản mới

Upstream: https://github.com/teng-lin/notebooklm-py — bấm **Watch → Releases** để nhận thông báo.

```bash
# 1. Xem bản mới nhất
pip index versions notebooklm-py        # hoặc xem trên PyPI

# 2. Sửa pin trong requirements.txt (ví dụ 0.8.5 → 0.9.0)
#    Đợi ~1 tuần sau release mới để tránh bản lỗi/yanked.

# 3. Rebuild — giữ nguyên volume nên session KHÔNG mất
docker compose up -d --build

# 4. Verify
docker exec nlm-bridge pip show notebooklm-py
curl http://localhost:8767/health
curl -H "Authorization: Bearer <t>" http://localhost:8767/status?profile=p1
```

**Rollback**: `git checkout` commit cũ (hoặc sửa lại pin) → `docker compose up -d --build`. Session trong `/data` không bị ảnh hưởng.

**Mock mode** test contract không cần Google: thêm `NLM_MOCK=1` vào environment — mọi endpoint trả dữ liệu giả.

## Biến môi trường

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `NLM_BRIDGE_TOKEN` | `test-secret-token` ⚠️ | Bearer token — **đổi ngay khi deploy** |
| `NLM_STORAGE_DIR` | `/data/profiles` | Thư mục chứa session profiles trong container |
| `NLM_ARTIFACT_DIR` | `/tmp/nlm-artifacts` | Nơi đệm file MP3/JSON trước khi trả về |
| `NLM_MOCK` | — | `1` = mock mode, không gọi Google |
| `NLM_HOST_DIR` | `$HOME/.notebooklm` | (compose local) thư mục host bind-mount vào `/data` |

## Troubleshooting

| Triệu chứng | Nguyên nhân / xử lý |
|---|---|
| `auth_missing` / `no_credentials` | Profile chưa có session → `push-profile.sh` hoặc `POST /auth` |
| `invalid_credentials` sau upload | Cookie hết hạn/thiếu — export lại từ trình duyệt đã login `notebooklm.google.com` |
| `upstream_error` trên `/artifact` | NotebookLM không nhận source — URL phải public, không bị chặn bot |
| `/artifact/status` mãi `pending` | Google đang render — audio/quiz mất vài phút, poll tiếp |
| Sau rebuild mất login | Đã đổi/xoá mount `/data` — đảm bảo bind-mount `$HOME/.notebooklm` hoặc volume `nlm-data` còn nguyên |
| `401 unauthorized` | `NLM_BRIDGE_TOKEN` trên bridge ≠ token client gửi — check `.env` |

## Bảo mật

- Đổi `NLM_BRIDGE_TOKEN` thành chuỗi ngẫu nhiên ≥32 ký tự trước khi mở port ra internet
- Luôn đặt sau HTTPS khi expose ra ngoài — token là bearer, ai có token dùng được tài khoản Google của bạn
- `storage_state.json` chứa cookie Google — không commit, không expose; `.gitignore` đã chặn `profiles/`
- Không chạy `NLM_MOCK=1` trên production

## Stack

Python 3.12 · FastAPI · uvicorn · `notebooklm-py==0.8.5` (pin trong `requirements.txt`)
