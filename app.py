"""NotebookLM bridge for the AI SEO Writer plugin.

Implements the plugin's AIBGResearchClient contract:

    POST /research   Authorization: Bearer <NLM_BRIDGE_TOKEN>
    {"keyword": str, "urls": [str], "profile": str, "mode": "fast"|"deep", "timeout": int}
    -> {"status": "success", "profile": ..., "mode": ...,
        "evidence_pack": {
            # ask 1 — evidence core
            "hard_numbers": [{"metric","value"}],
            "claims": [{"claim","citations":[{"source_id"}]}],
            "conflicts": [{"topic","detail"}], "knowledge_gaps": [],
            "consensus": [], "divergent": [{"topic","positions":[{"stance","sources"}]}],
            "echo_chamber": [], "entities": [{"term","type"}],
            "actionable": {"steps":[],"tools":[],"prices":[],"dos":[],"donts":[]},
            # ask 2 — gap analysis (same conversation, best-effort)
            "coverage_matrix": [{"subtopic","covered_by":[],"depth"}],
            "unanswered_questions": [],
            "freshness": {"outdated":[{"claim","source","date"}],
                          "latest_dates":[],"recency_gap":""},
            "unique_to_source": [{"source","point"}], "format_gaps": []}}

Real mode talks to NotebookLM via notebooklm-py (requires
~/.notebooklm/storage_state.json produced by a one-time browser login).
Set NLM_MOCK=1 to run the contract tests without Google credentials.
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse

BRIDGE_TOKEN = os.environ.get("NLM_BRIDGE_TOKEN", "dev-bridge-token")
MOCK = os.environ.get("NLM_MOCK") == "1"
MAX_URLS = 10
DEFAULT_TIMEOUT = 180
# NotebookLM allows ~50 sources per notebook. Keep headroom for deep-mode
# imports when pruning oldest sources before adding a fresh batch.
NOTEBOOK_SOURCE_CAP = 50
DEEP_HEADROOM = 5
# Per-profile storage, same layout as `notebooklm login` (profiles/<name>/storage_state.json)
STORAGE_DIR = Path(os.environ.get(
    "NLM_STORAGE_DIR", str(Path.home() / ".notebooklm" / "profiles")))


def _storage_path(profile: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", profile) or "default"
    return STORAGE_DIR / safe / "storage_state.json"

app = FastAPI(title="nlm-bridge", version="0.1.0")
_lock = asyncio.Lock()

EXTRACT_PROMPT = """Bạn là bộ phân tích nghiên cứu. Dựa trên các nguồn trong notebook, trích xuất evidence cho chủ đề: "{keyword}".

Chỉ trả về MỘT JSON object hợp lệ (không markdown, không giải thích) với đúng schema:
{{
  "hard_numbers": [{{"metric": "tên chỉ số", "value": "giá trị có đơn vị"}}],
  "claims": [{{"claim": "khẳng định xác thực được từ nguồn", "citations": [{{"source_id": "url hoặc tên nguồn"}}]}}],
  "conflicts": [{{"topic": "chủ đề mâu thuẫn", "detail": "nguồn A nói gì vs nguồn B nói gì"}}],
  "knowledge_gaps": ["thông tin quan trọng nguồn không đề cập"],
  "consensus": ["điểm phần lớn các nguồn đồng thuận — nội dung bắt buộc phải có trong bài"],
  "divergent": [{{"topic": "chủ đề nhiều quan điểm", "positions": [{{"stance": "quan điểm", "sources": ["url"]}}]}}],
  "echo_chamber": ["khẳng định được lặp lại ở nhiều nguồn nhưng không nguồn nào dẫn chứng gốc"],
  "entities": [{{"term": "thuật ngữ/thương hiệu/sản phẩm/khái niệm", "type": "brand|product|concept|person|tool"}}],
  "actionable": {{"steps": ["bước thực hành cụ thể"], "tools": ["công cụ được nhắc"], "prices": ["giá/khoảng giá"], "dos": ["việc nên làm"], "donts": ["việc cần tránh"]}}
}}
Mỗi mảng tối đa 10 mục, ưu tiên quan trọng nhất. Nếu không có dữ liệu cho một mảng, trả mảng rỗng."""

GAP_PROMPT = """Tiếp tục phân tích các nguồn trong notebook cho chủ đề: "{keyword}".

Chỉ trả về MỘT JSON object hợp lệ (không markdown, không giải thích) với đúng schema:
{{
  "coverage_matrix": [{{"subtopic": "chủ đề con quan trọng", "covered_by": ["url nguồn đề cập"], "depth": "deep|shallow"}}],
  "unanswered_questions": ["câu hỏi người đọc quan tâm mà không nguồn nào trả lời trọn vẹn"],
  "freshness": {{"outdated": [{{"claim": "thông tin lỗi thời", "source": "url", "date": "mốc thời gian cũ"}}], "latest_dates": ["mốc thời gian mới nhất được đề cập"], "recency_gap": "nhận xét độ mới dữ liệu chung của các nguồn"}},
  "unique_to_source": [{{"source": "url", "point": "nội dung chỉ nguồn này có"}}],
  "format_gaps": ["định dạng nội dung các nguồn còn thiếu: bảng so sánh, checklist, hướng dẫn từng bước, FAQ, video…"]
}}
coverage_matrix phải liệt kê đủ các subtopic quan trọng của chủ đề, kể cả subtopic có covered_by rỗng (0 nguồn đề cập — khoảng trống lớn nhất để bài viết lấp vào). Mỗi mảng tối đa 12 mục. Không có dữ liệu → mảng rỗng."""


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "status": "error", "error": {"code": code, "message": message}})


def _strs(raw, limit: int = 15) -> list:
    return [str(v).strip() for v in (raw or [])
            if isinstance(v, (str, int, float)) and str(v).strip()][:limit]


def _normalize_pack(raw) -> dict:
    """Evidence core (ask 1). Plugin consumers read hard_numbers/claims/
    conflicts{topic,detail}/knowledge_gaps — keep those shapes stable."""
    pack = {
        "hard_numbers": [], "claims": [], "conflicts": [], "knowledge_gaps": [],
        "consensus": [], "divergent": [], "echo_chamber": [],
        "entities": [], "actionable": {},
    }
    if not isinstance(raw, dict):
        return pack
    for item in raw.get("hard_numbers") or []:
        if isinstance(item, dict) and item.get("metric") is not None:
            pack["hard_numbers"].append({
                "metric": str(item["metric"]), "value": str(item.get("value", ""))})
    for item in raw.get("claims") or []:
        if isinstance(item, dict) and item.get("claim"):
            cits = [{"source_id": str(c.get("source_id", ""))}
                    for c in (item.get("citations") or []) if isinstance(c, dict)]
            pack["claims"].append({"claim": str(item["claim"]), "citations": cits})
    for item in raw.get("conflicts") or []:
        if isinstance(item, dict):
            topic, detail = str(item.get("topic") or ""), str(item.get("detail") or "")
            if topic or detail:
                pack["conflicts"].append({"topic": topic, "detail": detail})
        elif isinstance(item, str) and item.strip():
            pack["conflicts"].append({"topic": "", "detail": item.strip()})
    pack["knowledge_gaps"] = _strs(raw.get("knowledge_gaps"))
    pack["consensus"] = _strs(raw.get("consensus"))
    pack["echo_chamber"] = _strs(raw.get("echo_chamber"))
    for item in raw.get("divergent") or []:
        if not isinstance(item, dict) or not item.get("topic"):
            continue
        positions = []
        for pos in item.get("positions") or []:
            if isinstance(pos, dict) and pos.get("stance"):
                positions.append({"stance": str(pos["stance"]),
                                  "sources": _strs(pos.get("sources"), 5)})
        pack["divergent"].append({"topic": str(item["topic"]), "positions": positions})
    for item in raw.get("entities") or []:
        if isinstance(item, dict) and item.get("term"):
            pack["entities"].append({"term": str(item["term"]),
                                     "type": str(item.get("type") or "")})
        elif isinstance(item, str) and item.strip():
            pack["entities"].append({"term": item.strip(), "type": ""})
    act = raw.get("actionable")
    if isinstance(act, dict):
        pack["actionable"] = {
            k: _strs(act.get(k)) for k in ("steps", "tools", "prices", "dos", "donts")}
    return pack


def _normalize_gap(raw) -> dict:
    """Gap analysis (ask 2): coverage/questions/freshness/uniqueness/format."""
    pack = {
        "coverage_matrix": [], "unanswered_questions": [],
        "freshness": {}, "unique_to_source": [], "format_gaps": [],
    }
    if not isinstance(raw, dict):
        return pack
    for item in raw.get("coverage_matrix") or []:
        if not isinstance(item, dict) or not item.get("subtopic"):
            continue
        pack["coverage_matrix"].append({
            "subtopic": str(item["subtopic"]),
            "covered_by": _strs(item.get("covered_by"), 10),
            "depth": str(item.get("depth") or ""),
        })
    pack["unanswered_questions"] = _strs(raw.get("unanswered_questions"))
    fresh = raw.get("freshness")
    if isinstance(fresh, dict):
        outdated = []
        for item in fresh.get("outdated") or []:
            if isinstance(item, dict) and item.get("claim"):
                outdated.append({
                    "claim": str(item["claim"]),
                    "source": str(item.get("source") or ""),
                    "date": str(item.get("date") or ""),
                })
        pack["freshness"] = {
            "outdated": outdated,
            "latest_dates": _strs(fresh.get("latest_dates"), 10),
            "recency_gap": str(fresh.get("recency_gap") or ""),
        }
    for item in raw.get("unique_to_source") or []:
        if isinstance(item, dict) and item.get("point"):
            pack["unique_to_source"].append({
                "source": str(item.get("source") or ""),
                "point": str(item["point"]),
            })
    pack["format_gaps"] = _strs(raw.get("format_gaps"))
    return pack


def _extract_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


async def _real_research(keyword: str, urls: list, profile: str, mode: str,
                         timeout: int) -> tuple[dict, bool]:
    from notebooklm import NotebookLMClient
    client = await NotebookLMClient.from_storage(_storage_path(profile))
    try:
        async with client:
            title = f"ai-seo:{profile}"
            notebooks = await client.notebooks.list()
            nb = next((n for n in notebooks if getattr(n, "title", "") == title), None)
            if nb is None:
                nb = await client.notebooks.create(title)
            nb_id = getattr(nb, "id", None) or getattr(nb, "notebook_id")
            # Prune oldest sources when the notebook nears NotebookLM's
            # 50-source cap — otherwise ADD_SOURCE starts failing with
            # rpc_code=9 and research silently degrades.
            batch_urls = urls[:MAX_URLS]
            try:
                existing = await client.sources.list(nb_id)
            except Exception:
                existing = []
            over = len(existing) + len(batch_urls) + DEEP_HEADROOM - NOTEBOOK_SOURCE_CAP
            pruned = 0
            if over > 0 and existing:
                existing.sort(key=lambda s: str(getattr(s, "created_at", "") or ""))
                for old in existing[:over]:
                    try:
                        await client.sources.delete(nb_id, getattr(old, "id", ""))
                        pruned += 1
                    except Exception:
                        continue
            scoped_ids: list[str] = []
            for url in batch_urls:
                try:
                    src = await client.sources.add_url(nb_id, url, wait=True,
                                                       wait_timeout=min(120, timeout))
                    sid = getattr(src, "id", None) or getattr(src, "source_id", None)
                    if sid:
                        scoped_ids.append(str(sid))
                except Exception:
                    continue  # a bad source must not sink the whole research call
            if mode == "deep":
                try:
                    task = await client.research.start(nb_id, keyword, source="web", mode="deep")
                    if task and getattr(task, "get", None):
                        task_id = task.get("task_id") or task.get("id")
                        deadline = time.monotonic() + timeout
                        while task_id and time.monotonic() < deadline:
                            st = await client.research.poll(nb_id)
                            if isinstance(st, dict) and st.get("status") in ("completed", "done"):
                                imported = await client.research.import_sources(
                                    nb_id, task_id, st.get("sources", []))
                                if isinstance(imported, list):
                                    for it in imported:
                                        if isinstance(it, dict):
                                            sid = it.get("source_id") or it.get("id")
                                            if sid:
                                                scoped_ids.append(str(sid))
                                break
                            await asyncio.sleep(5)
                except Exception:
                    pass  # deep research is best-effort; chat still works on urls
            # Fresh conversation per keyword — a null conversation_id would
            # continue the previous keyword's chat and bleed its context in.
            try:
                conv_id = await client.chat.get_conversation_id(nb_id)
                if conv_id:
                    await client.chat.delete_conversation(nb_id, conv_id)
            except Exception:
                pass
            ask_sources = scoped_ids or None
            answer = await client.chat.ask(nb_id, EXTRACT_PROMPT.format(keyword=keyword),
                                           source_ids=ask_sources)
            text = (getattr(answer, "answer", None) or getattr(answer, "text", None)
                    or str(answer))
            pack = _normalize_pack(_extract_json(text))
            # Ask 2 continues the same conversation — gap analysis on the
            # already-ingested sources. Best-effort: a failure here must not
            # sink the core evidence pack.
            pack.update(_normalize_gap(None))
            gap_ok = False
            try:
                gap_answer = await client.chat.ask(nb_id, GAP_PROMPT.format(keyword=keyword),
                                                   source_ids=ask_sources)
                gap_text = (getattr(gap_answer, "answer", None)
                            or getattr(gap_answer, "text", None) or str(gap_answer))
                gap = _normalize_gap(_extract_json(gap_text))
                pack.update(gap)
                gap_ok = any([
                    gap["coverage_matrix"], gap["unanswered_questions"],
                    gap["freshness"], gap["unique_to_source"], gap["format_gaps"],
                ])
            except Exception:
                pass
            print(f"[research] profile={profile} mode={mode} asks={'2' if gap_ok else '1'} "
                  f"sources={len(scoped_ids)} pruned={pruned}", flush=True)
            return pack, gap_ok
    finally:
        try:
            await client.close()
        except Exception:
            pass


@app.get("/health")
async def health():
    return {"ok": True, "mock": MOCK}


def _authed(request: Request) -> bool:
    return request.headers.get("authorization", "") == f"Bearer {BRIDGE_TOKEN}"


async def _check_auth(profile: str = "default") -> dict:
    """Validate saved credentials by hitting the real NotebookLM API."""
    path = _storage_path(profile)
    if not path.exists():
        return {"authenticated": False, "reason": "no_credentials", "profile": profile}
    try:
        from notebooklm import NotebookLMClient
        client = await NotebookLMClient.from_storage(path)
        async with client:
            email = await client.get_account_email()
            return {"authenticated": True, "email": email, "profile": profile}
    except Exception as e:
        return {"authenticated": False,
                "reason": f"{type(e).__name__}: {e}", "profile": profile}


def _to_storage_state(body) -> dict | None:
    """Accept: Playwright storage_state {cookies:[...]}, a bare cookie array
    (Cookie-Editor export), or a raw 'k=v; k=v' Cookie header string."""
    cookies = None
    if isinstance(body, dict) and isinstance(body.get("cookies"), list):
        cookies = body["cookies"]
    elif isinstance(body, list):
        cookies = body
    elif isinstance(body, str):
        cookies = []
        for pair in body.split(";"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                k = k.strip()
                if k:
                    cookies.append({"name": k, "value": v.strip(),
                                    "domain": ".google.com", "path": "/"})
    if not cookies:
        return None
    norm = []
    for c in cookies:
        if isinstance(c, dict) and c.get("name") is not None and c.get("value") is not None:
            norm.append({
                "name": str(c["name"]), "value": str(c["value"]),
                "domain": str(c.get("domain") or ".google.com"),
                "path": str(c.get("path") or "/"),
                "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", True)),
            })
    return {"cookies": norm, "origins": body.get("origins", []) if isinstance(body, dict) else []} \
        if norm else None


@app.get("/status")
async def status(request: Request):
    if not _authed(request):
        return _err(401, "unauthorized", "invalid bearer token")
    profile = request.query_params.get("profile") or "default"
    if MOCK:
        return {"authenticated": True, "email": "mock@example.test",
                "profile": profile, "mock": True}
    return await asyncio.wait_for(_check_auth(profile), timeout=30)


@app.get("/profiles")
async def profiles(request: Request):
    if not _authed(request):
        return _err(401, "unauthorized", "invalid bearer token")
    out = []
    if STORAGE_DIR.exists():
        for p in sorted(STORAGE_DIR.iterdir()):
            if (p / "storage_state.json").exists():
                out.append(p.name)
    return {"profiles": out}


@app.post("/auth")
async def auth_upload(request: Request):
    """Accept login state from the user's own browser — no CLI needed:
    Playwright storage_state JSON, Cookie-Editor export, or a raw Cookie
    header string. Saves per-profile and validates against the live API."""
    if not _authed(request):
        return _err(401, "unauthorized", "invalid bearer token")
    try:
        body = await request.json()
    except Exception:
        return _err(400, "bad_json", "body must be JSON")
    profile = "default"
    payload = body
    if isinstance(body, dict) and "profile" in body:
        profile = str(body.get("profile") or "default")
    if profile.lower() == "auto":
        n = 1
        while _storage_path(f"p{n}").exists():
            n += 1
        profile = f"p{n}"
        if "storage_state" in body:
            payload = body["storage_state"]
        elif isinstance(body.get("cookies"), list):
            payload = {"cookies": body["cookies"]}
    state = _to_storage_state(payload)
    if state is None:
        return _err(400, "bad_request",
                    "expected storage_state {cookies:[...]}, a Cookie-Editor array, or a 'k=v; …' Cookie header")
    path = _storage_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)
    if MOCK:
        return {"status": "success", "email": "mock@example.test",
                "profile": profile, "mock": True}
    try:
        result = await asyncio.wait_for(_check_auth(profile), timeout=30)
    except asyncio.TimeoutError:
        return _err(504, "timeout", "validation timed out")
    if not result.get("authenticated"):
        return _err(400, "invalid_credentials",
                    str(result.get("reason", "unknown")))
    return {"status": "success", "email": result.get("email"), "profile": profile}


@app.post("/research")
async def research(request: Request):
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {BRIDGE_TOKEN}":
        return _err(401, "unauthorized", "invalid bearer token")
    try:
        body = await request.json()
    except Exception:
        return _err(400, "bad_json", "request body must be a JSON object")
    if not isinstance(body, dict):
        return _err(400, "bad_json", "request body must be a JSON object")
    keyword = body.get("keyword")
    if not isinstance(keyword, str) or not keyword.strip():
        return _err(400, "bad_request", "keyword is required")
    urls = body.get("urls") or []
    if not isinstance(urls, list):
        return _err(400, "bad_request", "urls must be an array")
    urls = [str(u) for u in urls if isinstance(u, str) and u.strip()][:MAX_URLS]
    profile = str(body.get("profile") or "p1")
    mode = body.get("mode") if body.get("mode") in ("fast", "deep") else "fast"
    timeout = max(30, min(600, int(body.get("timeout") or DEFAULT_TIMEOUT)))

    if MOCK:
        pack = {"hard_numbers": [{"metric": "mock metric", "value": "42%"}],
                "claims": [{"claim": f"mock claim for {keyword}",
                            "citations": [{"source_id": "mock://source-1"}]}],
                "conflicts": [], "knowledge_gaps": ["mock gap"],
                "consensus": [], "divergent": [], "echo_chamber": [],
                "entities": [], "actionable": {},
                "coverage_matrix": [], "unanswered_questions": [],
                "freshness": {}, "unique_to_source": [], "format_gaps": []}
        return {"status": "success", "profile": profile, "mode": mode,
                "evidence_pack": pack}

    async with _lock:
        try:
            pack, gap_ok = await asyncio.wait_for(
                _real_research(keyword, urls, profile, mode, timeout),
                timeout=timeout + 30)
        except asyncio.TimeoutError:
            return _err(504, "timeout", "notebooklm research timed out")
        except FileNotFoundError:
            return _err(502, "auth_missing",
                        "~/.notebooklm/storage_state.json not found — run the login flow")
        except Exception as e:
            return _err(502, "upstream_error", f"{type(e).__name__}: {e}")
    return {"status": "success", "profile": profile, "mode": mode,
            "asks": 2 if gap_ok else 1,
            "evidence_pack": pack}


# --------------------------------------------------------------------------
# Studio artifacts: Audio Overview + Quiz. Generation is slow (minutes), so
# the flow is: POST /artifact -> task ids, GET /artifact/status -> poll,
# GET /artifact/download -> file/json once completed.
# --------------------------------------------------------------------------

_ARTIFACT_KINDS = ("audio", "quiz")
_ARTIFACT_DIR = Path(os.environ.get("NLM_ARTIFACT_DIR", "/tmp/nlm-artifacts"))


async def _artifact_notebook(client, profile: str) -> str:
    title = f"ai-seo:{profile}"
    notebooks = await client.notebooks.list()
    nb = next((n for n in notebooks if getattr(n, "title", "") == title), None)
    if nb is None:
        nb = await client.notebooks.create(title)
    return getattr(nb, "id", None) or getattr(nb, "notebook_id")


async def _artifact_add_sources(client, nb_id: str, urls: list, timeout: int) -> list:
    scoped_ids: list[str] = []
    for url in urls[:MAX_URLS]:
        try:
            src = await client.sources.add_url(nb_id, url, wait=True,
                                               wait_timeout=min(120, timeout))
            sid = getattr(src, "id", None) or getattr(src, "source_id", None)
            if sid:
                scoped_ids.append(str(sid))
        except Exception:
            continue
    return scoped_ids


async def _artifact_add_text(client, nb_id: str, title: str, content: str,
                             timeout: int) -> list:
    """Paste article text as the source — works for localhost/private sites
    and pages behind bot protection where add_url would silently fail."""
    try:
        src = await client.sources.add_text(nb_id, title[:250], content,
                                            wait=True,
                                            wait_timeout=min(120, timeout))
        sid = getattr(src, "id", None) or getattr(src, "source_id", None)
        return [str(sid)] if sid else []
    except Exception:
        return []


def _artifact_task_id(status) -> str | None:
    return (getattr(status, "task_id", None) or getattr(status, "id", None)
            or getattr(status, "artifact_id", None))


@app.post("/artifact")
async def artifact_start(request: Request):
    """Kick off artifact generation and return immediately with task ids.
    {"kind": "audio"|"quiz"|"both", "urls": [permalink...], "profile": str,
     "title": str, "content": str (article text — preferred source, works for
     private/localhost URLs), "lang": "vi",
     "audio_format": "brief|deep_dive|critique|debate",
     "audio_length": "short|default|long",
     "audio_instructions": str (Audio Overview "customize" prompt — steers
      style/tone consistently across posts),
     "quiz_quantity": "fewer|standard|more",
     "quiz_difficulty": "easy|medium|hard"}"""
    if not _authed(request):
        return _err(401, "unauthorized", "invalid bearer token")
    try:
        body = await request.json()
    except Exception:
        return _err(400, "bad_json", "request body must be a JSON object")
    if not isinstance(body, dict):
        return _err(400, "bad_json", "request body must be a JSON object")
    kind = str(body.get("kind") or "audio").lower()
    kinds = _ARTIFACT_KINDS if kind == "both" else (kind,)
    if any(k not in _ARTIFACT_KINDS for k in kinds):
        return _err(400, "bad_request", "kind must be audio|quiz|both")
    urls = [str(u) for u in (body.get("urls") or [])
            if isinstance(u, str) and u.strip()][:MAX_URLS]
    content = str(body.get("content") or "")[:200000]
    title = str(body.get("title") or "ai-seo source")
    if not urls and not content.strip():
        return _err(400, "bad_request",
                    "urls or content is required (article permalink/text)")
    profile = str(body.get("profile") or "p1")
    lang = str(body.get("lang") or "en")
    timeout = max(30, min(600, int(body.get("timeout") or DEFAULT_TIMEOUT)))

    if MOCK:
        return {"status": "success", "profile": profile,
                "tasks": {k: f"mock-{k}-task" for k in kinds}}

    async def _run() -> dict:
        from notebooklm import NotebookLMClient
        client = await NotebookLMClient.from_storage(_storage_path(profile))
        tasks: dict = {}
        try:
            async with client:
                nb_id = await _artifact_notebook(client, profile)
                scoped_ids = []
                if content.strip():
                    scoped_ids += await _artifact_add_text(
                        client, nb_id, title, content, timeout)
                if urls:
                    scoped_ids += await _artifact_add_sources(
                        client, nb_id, urls, timeout)
                src = scoped_ids or None
                if "audio" in kinds:
                    from notebooklm import AudioFormat, AudioLength
                    fmt_map = {"brief": AudioFormat.BRIEF,
                               "deep_dive": AudioFormat.DEEP_DIVE,
                               "critique": AudioFormat.CRITIQUE,
                               "debate": AudioFormat.DEBATE}
                    len_map = {"short": AudioLength.SHORT,
                               "default": AudioLength.DEFAULT,
                               "long": AudioLength.LONG}
                    fmt = fmt_map.get(str(body.get("audio_format") or "brief").lower(),
                                      AudioFormat.BRIEF)
                    length = len_map.get(str(body.get("audio_length") or "short").lower(),
                                         AudioLength.SHORT)
                    instructions = str(
                        body.get("audio_instructions") or "").strip()[:4000] or None
                    st = await client.artifacts.generate_audio(
                        nb_id, source_ids=src, audio_format=fmt,
                        audio_length=length, language=lang,
                        instructions=instructions)
                    tid = _artifact_task_id(st)
                    if tid:
                        tasks["audio"] = tid
                if "quiz" in kinds:
                    from notebooklm import QuizQuantity, QuizDifficulty
                    q_map = {"fewer": QuizQuantity.FEWER,
                             "standard": QuizQuantity.STANDARD,
                             "more": QuizQuantity.MORE}
                    d_map = {"easy": QuizDifficulty.EASY,
                             "medium": QuizDifficulty.MEDIUM,
                             "hard": QuizDifficulty.HARD}
                    qty = q_map.get(str(body.get("quiz_quantity") or "standard").lower(),
                                    QuizQuantity.STANDARD)
                    diff = d_map.get(str(body.get("quiz_difficulty") or "medium").lower(),
                                     QuizDifficulty.MEDIUM)
                    st = await client.artifacts.generate_quiz(
                        nb_id, source_ids=src, quantity=qty, difficulty=diff)
                    tid = _artifact_task_id(st)
                    if tid:
                        tasks["quiz"] = tid
        finally:
            try:
                await client.close()
            except Exception:
                pass
        return tasks

    async with _lock:
        try:
            tasks = await asyncio.wait_for(_run(), timeout=timeout + 30)
        except asyncio.TimeoutError:
            return _err(504, "timeout", "artifact start timed out")
        except FileNotFoundError:
            return _err(502, "auth_missing",
                        "storage_state.json not found — run the login flow")
        except Exception as e:
            return _err(502, "upstream_error", f"{type(e).__name__}: {e}")
    if not tasks:
        return _err(502, "upstream_error", "no artifact task accepted")
    return {"status": "success", "profile": profile, "tasks": tasks}


async def _artifact_status(client, nb_id: str, task_id: str) -> dict:
    st = await client.artifacts.poll_status(nb_id, task_id)
    state = (getattr(st, "status", None) or getattr(st, "state", None)
             or str(st)).lower()
    if "complete" in state or state in ("done", "finished", "succeeded"):
        state = "completed"
    elif "fail" in state or "error" in state:
        state = "failed"
    elif "progress" in state or "running" in state:
        state = "in_progress"
    else:
        state = "pending"
    return {"status": state,
            "artifact_id": getattr(st, "artifact_id", None) or task_id}


@app.get("/artifact/status")
async def artifact_status(request: Request):
    if not _authed(request):
        return _err(401, "unauthorized", "invalid bearer token")
    task_id = request.query_params.get("task_id") or ""
    profile = request.query_params.get("profile") or "p1"
    if not task_id:
        return _err(400, "bad_request", "task_id is required")
    if MOCK:
        return {"status": "completed", "artifact_id": task_id, "mock": True}

    from notebooklm import NotebookLMClient
    async with _lock:
        try:
            client = await NotebookLMClient.from_storage(_storage_path(profile))
            async with client:
                nb_id = await _artifact_notebook(client, profile)
                return await _artifact_status(client, nb_id, task_id)
        except FileNotFoundError:
            return _err(502, "auth_missing", "storage_state.json not found")
        except Exception as e:
            return _err(502, "upstream_error", f"{type(e).__name__}: {e}")


@app.get("/artifact/download")
async def artifact_download(request: Request):
    """?task_id=&kind=audio|quiz&profile= — audio returns the binary file,
    quiz returns parsed JSON."""
    if not _authed(request):
        return _err(401, "unauthorized", "invalid bearer token")
    task_id = request.query_params.get("task_id") or ""
    kind = (request.query_params.get("kind") or "audio").lower()
    profile = request.query_params.get("profile") or "p1"
    if not task_id or kind not in _ARTIFACT_KINDS:
        return _err(400, "bad_request", "task_id + kind(audio|quiz) required")
    if MOCK:
        if kind == "quiz":
            return {"status": "success", "quiz": [
                {"question": "Mock question?", "options": ["A", "B"],
                 "answer_index": 0, "explanation": "mock"}]}
        return _err(400, "bad_request", "audio download not mocked")

    from notebooklm import NotebookLMClient
    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    safe_task = re.sub(r"[^A-Za-z0-9_-]", "", task_id) or "task"
    async with _lock:
        try:
            client = await NotebookLMClient.from_storage(_storage_path(profile))
            async with client:
                nb_id = await _artifact_notebook(client, profile)
                if kind == "audio":
                    out = _ARTIFACT_DIR / f"{safe_task}.mp3"
                    await client.artifacts.download_audio(
                        nb_id, str(out), artifact_id=task_id)
                    return FileResponse(str(out), media_type="audio/mpeg",
                                        filename=f"{safe_task}.mp3")
                out = _ARTIFACT_DIR / f"{safe_task}.json"
                await client.artifacts.download_quiz(
                    nb_id, str(out), artifact_id=task_id, output_format="json")
                try:
                    data = json.loads(out.read_text(encoding="utf-8"))
                except Exception:
                    data = {"raw": out.read_text(encoding="utf-8", errors="replace")}
                return {"status": "success", "quiz": data}
        except FileNotFoundError:
            return _err(502, "auth_missing", "storage_state.json not found")
        except Exception as e:
            return _err(502, "upstream_error", f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------
# /serp — adapter in front of the external AIBG/9r search service. The
# plugin posts its "aibg" payload {query,model,lang,country,count,
# exclude_domains}; we translate it to the upstream contract
# {model,query,search_type:"web",max_results} (Bearer = NLM_AIBG_KEY,
# `|`/`,` pool rotated round-robin) and normalize the reply into the shape
# AIBGSerpSearch reads:
# {results:[{position,url,title,snippet}], paa:[str], related:[str]}.
# This makes a localhost-only AIBG instance reachable from hosted WP.
# --------------------------------------------------------------------------

AIBG_URL = os.environ.get("NLM_AIBG_URL", "").strip().rstrip("/")
AIBG_KEYS = [k.strip() for k in re.split(r"[|,]", os.environ.get(
    "NLM_AIBG_KEY", "")) if k.strip()]
_aibg_key_cycle = None


def _next_aibg_key() -> str:
    global _aibg_key_cycle
    if not AIBG_KEYS:
        return ""
    if _aibg_key_cycle is None:
        import itertools
        _aibg_key_cycle = itertools.cycle(AIBG_KEYS)
    return next(_aibg_key_cycle)


def _host_of(url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    host = host.split(":")[0]  # strip port — upstreams exclude by host only
    return host[4:] if host.startswith("www.") else host


def _serp_normalize(data: dict, excluded: set) -> dict:
    # Tolerant mapping: unwrap common containers, accept the field names
    # seen across serper/scrappa/9r-style payloads.
    for wrap in ("data", "result", "response", "payload"):
        if isinstance(data.get(wrap), dict):
            data = data[wrap]
            break
    raw_results = (data.get("results") or data.get("organic")
                   or data.get("organic_results") or data.get("items")
                   or data.get("web") or [])
    results = []
    for i, it in enumerate(raw_results):
        if not isinstance(it, dict):
            continue
        link = str(it.get("url") or it.get("link") or it.get("href") or "")
        if _host_of(link) in excluded:
            continue
        pos = it.get("position") or it.get("rank")
        results.append({
            "position": int(pos) if isinstance(pos, (int, float))
                        or (isinstance(pos, str) and pos.isdigit()) else i + 1,
            "url": link,
            "title": str(it.get("title") or it.get("name") or ""),
            "snippet": str(it.get("snippet") or it.get("description")
                           or it.get("content") or it.get("text") or ""),
        })
    paa = []
    for x in (data.get("paa") or data.get("peopleAlsoAsk")
              or data.get("related_questions") or []):
        if isinstance(x, str) and x.strip():
            paa.append(x.strip())
        elif isinstance(x, dict) and x.get("question"):
            paa.append(str(x["question"]))
    related = []
    for x in (data.get("related") or data.get("relatedSearches")
              or data.get("related_searches") or []):
        if isinstance(x, str) and x.strip():
            related.append(x.strip())
        elif isinstance(x, dict) and x.get("query"):
            related.append(str(x["query"]))
    return {"results": results, "paa": paa, "related": related}


@app.post("/serp")
async def serp(request: Request):
    if not _authed(request):
        return _err(401, "unauthorized", "invalid bearer token")
    try:
        body = await request.json()
    except Exception:
        return _err(400, "bad_json", "request body must be a JSON object")
    if not isinstance(body, dict):
        return _err(400, "bad_json", "request body must be a JSON object")
    query = str(body.get("query") or "").strip()
    if not query:
        return _err(400, "bad_request", "query is required")
    if not AIBG_URL:
        return _err(501, "not_configured",
                    "set NLM_AIBG_URL (and NLM_AIBG_KEY) on this bridge")
    lang = str(body.get("lang") or "vi")[:10]
    country = str(body.get("country") or "vn").lower()[:10]
    try:
        count = max(1, min(30, int(body.get("count") or 10)))
    except (TypeError, ValueError):
        count = 10
    excluded = {_host_of(str(d))
                for d in (body.get("exclude_domains") or []) if isinstance(d, str)}

    if MOCK:
        return {"results": [{"position": 1, "url": "https://example.com",
                             "title": "mock", "snippet": "mock snippet"}],
                "paa": ["mock paa"], "related": ["mock related"],
                "_meta": {"model": "mock-serp"}}

    import httpx
    # Upstream contract: {model, query, search_type:"web", max_results}
    fwd = {
        "model": str(body.get("model") or ""),
        "query": query,
        "search_type": "web",
        "max_results": count,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    aibg_key = _next_aibg_key()
    if aibg_key:
        headers["Authorization"] = f"Bearer {aibg_key}"
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            r = await http.post(AIBG_URL, json=fwd, headers=headers)
    except Exception as e:
        return _err(502, "upstream_error", f"{type(e).__name__}: {e}")
    if not 200 <= r.status_code < 300:
        detail = r.text[:300].strip()
        return _err(502, "upstream_error",
                    f"aibg HTTP {r.status_code}"
                    + (f": {detail}" if detail else ""))
    try:
        data = r.json()
    except Exception:
        return _err(502, "upstream_error", "aibg response not JSON")
    if not isinstance(data, dict):
        return _err(502, "upstream_error", "aibg response not JSON object")
    # Some gateways return HTTP 200 with an {"error": ...} body.
    if data.get("error") and not (data.get("results") or data.get("data")):
        return _err(502, "upstream_error",
                    f"aibg: {str(data['error'])[:300]}")
    out = _serp_normalize(data, excluded)
    out["_meta"] = {"provider": "aibg",
                    "model": fwd["model"],
                    "upstream": data.get("provider") or data.get("engine")}
    return out
