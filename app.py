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


def _err_cause(e: Exception) -> str:
    """'Type: msg' plus the chained cause — the library wraps raw RPC
    failures (SourceAddError etc.) in friendlier messages that hide the
    real wire error unless we walk __cause__."""
    parts = [f"{type(e).__name__}: {e}"]
    seen = {id(e)}
    cur = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
    while cur is not None and id(cur) not in seen and len(parts) < 3:
        seen.add(id(cur))
        parts.append(f"{type(cur).__name__}: {cur}")
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    return " <- ".join(parts)[:600]


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
                            st_state = (str(st.get("status") or "").lower()
                                        if isinstance(st, dict) else "")
                            if st_state in ("completed", "done"):
                                imported = await client.research.import_sources(
                                    nb_id, task_id, st.get("sources", []))
                                if isinstance(imported, list):
                                    for it in imported:
                                        if isinstance(it, dict):
                                            sid = it.get("source_id") or it.get("id")
                                            if sid:
                                                scoped_ids.append(str(sid))
                                break
                            # Terminal-but-not-completed states must stop the
                            # loop — polling a dead task burns the whole budget.
                            if st_state in ("failed", "error", "cancelled",
                                            "canceled"):
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


@app.get("/version")
async def version():
    # app.py is COPY-baked into the image — its mtime inside the container is
    # the fingerprint of the deployed code, so a redeploy can be verified
    # without shell access. NLM_BUILD_SHA can pin an exact commit when set.
    try:
        mtime = int(os.path.getmtime(__file__))
    except OSError:
        mtime = 0
    return {"app_mtime": mtime,
            "build_sha": os.environ.get("NLM_BUILD_SHA", "")}


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
            return _err(502, "upstream_error", _err_cause(e))
    return {"status": "success", "profile": profile, "mode": mode,
            "asks": 2 if gap_ok else 1,
            "evidence_pack": pack}


# --------------------------------------------------------------------------
# Studio artifacts: Audio Overview + Quiz + Video Overview. Generation is
# slow (minutes; video up to ~1h for cinematic), so the flow is:
# POST /artifact -> task ids, GET /artifact/status -> poll,
# GET /artifact/download -> file/json once completed.
# --------------------------------------------------------------------------

_ARTIFACT_KINDS = ("audio", "quiz", "video")


def _parse_kinds(raw_kind: str) -> tuple | None:
    """Accept "both" (audio+quiz, legacy), "all", or a comma/pipe list like
    "audio,video" so callers opt into any subset explicitly."""
    raw_kind = raw_kind.lower().strip()
    if raw_kind == "both":
        return ("audio", "quiz")
    if raw_kind == "all":
        return _ARTIFACT_KINDS
    kinds = tuple(k for k in re.split(r"[,|]", raw_kind) if k)
    if not kinds or any(k not in _ARTIFACT_KINDS for k in kinds):
        return None
    return kinds


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
    first_err: Exception | None = None
    for url in urls[:MAX_URLS]:
        try:
            src = await client.sources.add_url(nb_id, url, wait=True,
                                               wait_timeout=min(120, timeout))
            sid = getattr(src, "id", None) or getattr(src, "source_id", None)
            if sid:
                scoped_ids.append(str(sid))
        except Exception as e:
            if first_err is None:
                first_err = e
            continue
    if urls and not scoped_ids and first_err is not None:
        raise RuntimeError(f"add_url failed for all sources: "
                           f"{type(first_err).__name__}: {first_err}")
    return scoped_ids


async def _artifact_add_text(client, nb_id: str, title: str, content: str,
                             timeout: int) -> list:
    """Paste article text as the source — works for localhost/private sites
    and pages behind bot protection where add_url would silently fail.
    Raises on failure: silently returning [] would unscope generation to
    the WHOLE shared notebook (all previously uploaded posts)."""
    src = await client.sources.add_text(nb_id, title[:250], content,
                                        wait=True,
                                        wait_timeout=min(120, timeout))
    sid = getattr(src, "id", None) or getattr(src, "source_id", None)
    if not sid:
        raise RuntimeError("add_text returned no source id")
    return [str(sid)]


def _artifact_task_id(status) -> str | None:
    return (getattr(status, "task_id", None) or getattr(status, "id", None)
            or getattr(status, "artifact_id", None))


def _collect_task(tasks: dict, rejected: dict, kind: str, status) -> None:
    """Record a generation kickoff: a task id whose initial state is already
    FAILED (quota) is rejected now instead of being queued as a dead task."""
    tid = _artifact_task_id(status)
    if not tid:
        return
    st_state = str(getattr(status, "status", "") or "").lower()
    if "fail" in st_state:
        rejected[kind] = str(
            getattr(status, "error", None)
            or getattr(status, "error_code", None)
            or "generation rejected at kickoff")
    else:
        tasks[kind] = tid


_VIDEO_FORMATS = {
    "explainer": "EXPLAINER", "brief": "BRIEF",
    "cinematic": "CINEMATIC", "short": "SHORT",
}
_VIDEO_STYLES = {
    "auto": "AUTO_SELECT", "custom": "CUSTOM", "classic": "CLASSIC",
    "whiteboard": "WHITEBOARD", "kawaii": "KAWAII", "anime": "ANIME",
    "watercolor": "WATERCOLOR", "retro_print": "RETRO_PRINT",
    "heritage": "HERITAGE", "paper_craft": "PAPER_CRAFT",
}


async def _gen_video(client, nb_id: str, source_ids, lang: str,
                     lang_name: str, body: dict,
                     tasks: dict, rejected: dict) -> None:
    """Video Overview — validates the same constraints the library's
    normalize_video_prompt enforces, but reports them under `rejected` so a
    bad video combo never sinks sibling artifact kickoffs."""
    from notebooklm import VideoFormat, VideoStyle
    fmt_name = str(body.get("video_format") or "explainer").lower()
    style_name = str(body.get("video_style") or "auto").lower()
    if fmt_name not in _VIDEO_FORMATS:
        rejected["video"] = (
            f"unknown video_format '{fmt_name}' "
            f"(explainer|brief|cinematic|short)")
        return
    if style_name not in _VIDEO_STYLES:
        rejected["video"] = (
            f"unknown video_style '{style_name}' "
            f"({'|'.join(_VIDEO_STYLES)})")
        return
    video_format = getattr(VideoFormat, _VIDEO_FORMATS[fmt_name])
    video_style = getattr(VideoStyle, _VIDEO_STYLES[style_name])
    style_prompt = str(body.get("video_style_prompt") or "").strip()[:4000]
    # Mirror normalize_video_prompt in the library: cinematic rejects
    # style_prompt; short has a fixed visual style (no style, no prompt);
    # style_prompt only exists for video_style=custom.
    if video_format == VideoFormat.CINEMATIC and style_prompt:
        rejected["video"] = "video_style_prompt is not supported for cinematic videos"
        return
    if video_format == VideoFormat.SHORT:
        if style_name not in ("auto",) or style_prompt:
            rejected["video"] = (
                "video_style/video_style_prompt are not supported for "
                "short videos (fixed visual style)")
            return
        video_style = None
    if video_style == VideoStyle.CUSTOM and not style_prompt:
        rejected["video"] = "video_style_prompt is required when video_style=custom"
        return
    if style_prompt and video_style != VideoStyle.CUSTOM:
        rejected["video"] = "video_style_prompt requires video_style=custom"
        return
    # Same contract as quiz: non-English output needs an explicit directive —
    # for video it folds into `instructions` (the Studio "customize" field).
    instructions = str(body.get("video_instructions") or "").strip()[:4000]
    if lang and not lang.lower().startswith("en"):
        lang_dir = (f"Produce the narration, on-screen text, and captions "
                    f"entirely in {lang_name}.")
        instructions = f"{lang_dir} {instructions}".strip()
    st = await client.artifacts.generate_video(
        nb_id, source_ids=source_ids, language=lang,
        instructions=instructions or None,
        video_format=video_format, video_style=video_style,
        style_prompt=style_prompt or None)
    _collect_task(tasks, rejected, "video", st)


@app.post("/artifact")
async def artifact_start(request: Request):
    """Kick off artifact generation and return immediately with task ids.
    {"kind": "audio"|"quiz"|"video"|"both"|"all"|"audio,video" (comma/pipe
      list or aliases: both=audio+quiz, all=every kind),
     "urls": [permalink...], "profile": str,
     "title": str, "content": str (article text — preferred source, works for
     private/localhost URLs), "lang": "vi", "lang_name": "Vietnamese",
     "audio_format": "brief|deep_dive|critique|debate",
     "audio_length": "short|default|long",
     "audio_instructions": str (Audio Overview "customize" prompt — steers
      style/tone consistently across posts),
     "quiz_quantity": "fewer|standard|more",
     "quiz_difficulty": "easy|medium|hard",
     "quiz_instructions": str,
     "video_format": "explainer|brief|cinematic|short",
     "video_style": "auto|classic|whiteboard|kawaii|anime|watercolor|
      retro_print|heritage|paper_craft|custom",
     "video_style_prompt": str (required when video_style=custom),
     "video_instructions": str (Video Overview customize prompt)}"""
    if not _authed(request):
        return _err(401, "unauthorized", "invalid bearer token")
    try:
        body = await request.json()
    except Exception:
        return _err(400, "bad_json", "request body must be a JSON object")
    if not isinstance(body, dict):
        return _err(400, "bad_json", "request body must be a JSON object")
    kinds = _parse_kinds(str(body.get("kind") or "audio"))
    if kinds is None:
        return _err(400, "bad_request",
                    "kind must be audio|quiz|video|both|all or a comma list")
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
        rejected: dict = {}
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
                # Never let generation fall back to `source_ids=None` —
                # that resolves to EVERY source in the shared per-profile
                # notebook, so artifacts would be built from other posts.
                if not scoped_ids:
                    raise RuntimeError(
                        "source registration produced no source ids — "
                        "refusing to generate on the shared notebook")
                src = scoped_ids
                lang_name = str(body.get("lang_name") or "").strip() or lang

                # Per-kind isolation: a validation/quota error on one kind
                # must not abort siblings whose tasks were already created
                # upstream — record it under `rejected` instead of throwing.
                if "audio" in kinds:
                    try:
                        from notebooklm import AudioFormat, AudioLength
                        fmt_map = {"brief": AudioFormat.BRIEF,
                                   "deep_dive": AudioFormat.DEEP_DIVE,
                                   "critique": AudioFormat.CRITIQUE,
                                   "debate": AudioFormat.DEBATE}
                        len_map = {"short": AudioLength.SHORT,
                                   "default": AudioLength.DEFAULT,
                                   "long": AudioLength.LONG}
                        fmt = fmt_map.get(
                            str(body.get("audio_format") or "brief").lower(),
                            AudioFormat.BRIEF)
                        length = len_map.get(
                            str(body.get("audio_length") or "short").lower(),
                            AudioLength.SHORT)
                        instructions = str(
                            body.get("audio_instructions") or "").strip()[:4000] or None
                        st = await client.artifacts.generate_audio(
                            nb_id, source_ids=src, audio_format=fmt,
                            audio_length=length, language=lang,
                            instructions=instructions)
                        _collect_task(tasks, rejected, "audio", st)
                    except Exception as e:
                        rejected["audio"] = f"{type(e).__name__}: {e}"
                if "quiz" in kinds:
                    try:
                        from notebooklm import QuizQuantity, QuizDifficulty
                        q_map = {"fewer": QuizQuantity.FEWER,
                                 "standard": QuizQuantity.STANDARD,
                                 "more": QuizQuantity.MORE}
                        d_map = {"easy": QuizDifficulty.EASY,
                                 "medium": QuizDifficulty.MEDIUM,
                                 "hard": QuizDifficulty.HARD}
                        qty = q_map.get(
                            str(body.get("quiz_quantity") or "standard").lower(),
                            QuizQuantity.STANDARD)
                        diff = d_map.get(
                            str(body.get("quiz_difficulty") or "medium").lower(),
                            QuizDifficulty.MEDIUM)
                        # generate_quiz has no language param — language and
                        # any custom style both travel through `instructions`.
                        # Use the human language name when sent ("Vietnamese"
                        # lands better in prompt text than the "vi" code).
                        q_instructions = str(
                            body.get("quiz_instructions") or "").strip()[:4000]
                        if lang and not lang.lower().startswith("en"):
                            lang_dir = (f"Write every question, option, and "
                                        f"explanation entirely in {lang_name}.")
                            q_instructions = (
                                f"{lang_dir} {q_instructions}").strip()
                        st = await client.artifacts.generate_quiz(
                            nb_id, source_ids=src,
                            instructions=q_instructions or None,
                            quantity=qty, difficulty=diff)
                        _collect_task(tasks, rejected, "quiz", st)
                    except Exception as e:
                        rejected["quiz"] = f"{type(e).__name__}: {e}"
                if "video" in kinds:
                    try:
                        await _gen_video(client, nb_id, src, lang, lang_name,
                                         body, tasks, rejected)
                    except Exception as e:
                        rejected["video"] = f"{type(e).__name__}: {e}"
        finally:
            try:
                await client.close()
            except Exception:
                pass
        return {"tasks": tasks, "rejected": rejected,
                "source_ids": scoped_ids}

    async with _lock:
        try:
            result = await asyncio.wait_for(_run(), timeout=timeout + 30)
        except asyncio.TimeoutError:
            return _err(504, "timeout", "artifact start timed out")
        except FileNotFoundError:
            return _err(502, "auth_missing",
                        "storage_state.json not found — run the login flow")
        except Exception as e:
            return _err(502, "upstream_error", _err_cause(e))
    tasks = result["tasks"]
    rejected = result["rejected"]
    if not tasks:
        detail = "; ".join(f"{k}: {v}" for k, v in rejected.items()) \
            or "no artifact task accepted"
        return _err(502, "upstream_error", detail)
    resp = {"status": "success", "profile": profile, "tasks": tasks,
            "source_ids": result["source_ids"]}
    if rejected:
        resp["rejected"] = rejected
    return resp


async def _artifact_status(client, nb_id: str, task_id: str) -> dict:
    st = await client.artifacts.poll_status(nb_id, task_id)
    state = (getattr(st, "status", None) or getattr(st, "state", None)
             or str(st)).lower()
    if "complete" in state or state in ("done", "finished", "succeeded"):
        state = "completed"
    elif "not_found" in state or "notfound" in state or "missing" in state:
        # Library reports NOT_FOUND when the artifact is absent from the
        # studio listing — expected for seconds after kickoff, but if it
        # persists the generation was silently rejected (quota). Never
        # collapse it into "pending" or the caller polls a dead task.
        state = "not_found"
    elif "fail" in state or "error" in state:
        state = "failed"
    elif "progress" in state or "running" in state:
        state = "in_progress"
    else:
        state = "pending"
    out = {"status": state,
           "artifact_id": getattr(st, "artifact_id", None) or task_id}
    err = getattr(st, "error", None) or getattr(st, "error_code", None)
    if err:
        out["error"] = str(err)
    return out


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
            return _err(502, "upstream_error", _err_cause(e))


@app.post("/artifact/cleanup")
async def artifact_cleanup(request: Request):
    """{"profile": str, "source_ids": [str, ...]} — delete text sources the
    caller registered on the shared ai-seo:<profile> notebook. The notebook
    would otherwise accumulate one source per post and hit NotebookLM's
    per-notebook source cap; deletion is per-source best-effort."""
    if not _authed(request):
        return _err(401, "unauthorized", "invalid bearer token")
    try:
        body = await request.json()
    except Exception:
        return _err(400, "bad_json", "request body must be a JSON object")
    if not isinstance(body, dict):
        return _err(400, "bad_json", "request body must be a JSON object")
    profile = str(body.get("profile") or "p1")
    ids = [str(s) for s in (body.get("source_ids") or [])
           if isinstance(s, (str, int))][:50]
    if not ids:
        return _err(400, "bad_request", "source_ids is required")
    if MOCK:
        return {"status": "success", "deleted": len(ids)}

    from notebooklm import NotebookLMClient
    async with _lock:
        try:
            client = await NotebookLMClient.from_storage(_storage_path(profile))
            async with client:
                nb_id = await _artifact_notebook(client, profile)
                deleted = 0
                failed = []
                for sid in ids:
                    try:
                        await client.sources.delete(nb_id, sid)
                        deleted += 1
                    except Exception as e:
                        failed.append(f"{sid}: {type(e).__name__}")
                return {"status": "success", "deleted": deleted,
                        "failed": failed}
        except FileNotFoundError:
            return _err(502, "auth_missing", "storage_state.json not found")
        except Exception as e:
            return _err(502, "upstream_error", _err_cause(e))


# --------------------------------------------------------------------------
# Video post-processing — ffmpeg trim/watermark + optional R2 upload.
# All heavy lifting happens here on the bridge (not in PHP) so WordPress just
# receives either a processed MP4 or a JSON {"url": <r2>}.


class _ProcessError(Exception):
    pass


def _fparam(params, name: str, lo: float, hi: float, default: float) -> float:
    try:
        v = float(params.get(name) or default)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


async def _run_cmd(*argv: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = (stderr or b"").decode(errors="replace")[-400:]
        raise _ProcessError(f"{argv[0]} rc={proc.returncode}: {tail}")


async def _probe_video(path: Path) -> tuple:
    """(duration_seconds, width, height) via ffprobe."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise _ProcessError("ffprobe failed: "
                            + (stderr or b"").decode(errors="replace")[-300:])
    try:
        info = json.loads(out)
        stream = (info.get("streams") or [{}])[0]
        return (float(info["format"]["duration"]),
                int(stream.get("width") or 0), int(stream.get("height") or 0))
    except Exception as e:
        raise _ProcessError(f"ffprobe parse: {e}")


async def _fetch_watermark(url: str) -> Path:
    """Download the overlay image once per URL and cache it under the
    artifact dir — repeat posts reuse the cached copy."""
    if not re.match(r"^https?://", url or ""):
        raise _ProcessError("watermark must be a public http(s) URL")
    import hashlib
    import httpx
    dest = _ARTIFACT_DIR / f"wm-{hashlib.md5(url.encode()).hexdigest()[:16]}"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    # Many CDNs 403 the bare httpx UA — send a plain browser UA so image
    # hosts served behind Cloudflare/Wikimedia-style front ends respond.
    headers = {"User-Agent": "Mozilla/5.0 (compatible; nlm-bridge/1.0)"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers=headers) as http:
        r = await http.get(url)
        if r.status_code != 200:
            raise _ProcessError(f"watermark fetch HTTP {r.status_code}")
        if len(r.content) > 5 * 1024 * 1024:
            raise _ProcessError("watermark image >5MB")
        dest.write_bytes(r.content)
    return dest


_WM_POS_EXPR = {
    "tl": ("{m}", "{m}"),
    "tr": ("W-w-{m}", "{m}"),
    "bl": ("{m}", "H-h-{m}"),
    "br": ("W-w-{m}", "H-h-{m}"),
}


async def _process_video(src: Path, safe_task: str, params) -> tuple:
    """Trim + watermark via ffmpeg, optionally push to Cloudflare R2.
    Returns (path_to_serve_or_None, public_url_or_None)."""
    trim_start = _fparam(params, "trim_start", 0, 600, 0)
    trim_end = _fparam(params, "trim_end", 0, 600, 0)
    wm_url = (params.get("watermark") or "").strip()
    wm_pos = (params.get("wm_pos") or "br").lower()
    if wm_pos not in _WM_POS_EXPR:
        wm_pos = "br"
    wm_pct = _fparam(params, "wm_width", 5, 40, 15)
    wm_margin = int(_fparam(params, "wm_margin", 0, 200, 24))
    want_r2 = str(params.get("r2") or "") in ("1", "true", "yes")

    needs_ffmpeg = bool(wm_url) or trim_start > 0 or trim_end > 0
    out = src
    if needs_ffmpeg:
        duration, width, _ = await _probe_video(src)
        keep = duration - trim_start - trim_end
        if duration > 0 and keep <= 0.5:
            raise _ProcessError(
                f"trim removes the whole video (dur={duration:.1f}s, "
                f"trim={trim_start}+{trim_end}s)")
        argv = ["ffmpeg", "-y"]
        if trim_start > 0:
            argv += ["-ss", f"{trim_start:.2f}"]
        argv += ["-i", str(src)]
        if wm_url:
            wm = await _fetch_watermark(wm_url)
            argv += ["-i", str(wm)]
            wm_px = max(16, round(width * wm_pct / 100)) if width else 160
            x, y = _WM_POS_EXPR[wm_pos]
            argv += [
                "-filter_complex",
                f"[1:v]scale={wm_px}:-2[wm];[0:v][wm]overlay="
                f"{x.format(m=wm_margin)}:{y.format(m=wm_margin)}[v]",
                "-map", "[v]", "-map", "0:a?"]
        # -t is an *output* option — it must sit after every -i, otherwise
        # it applies to the next input (the watermark image) and the real
        # video keeps its full tail.
        if duration > 0 and keep < duration:
            argv += ["-t", f"{keep:.2f}"]
        # Re-encode for frame-accurate cuts and a clean overlay bake.
        argv += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                 "-c:a", "aac", "-b:a", "128k"]
        out = src.with_name(f"{safe_task}.proc.mp4")
        argv += [str(out)]
        await _run_cmd(*argv)

    if want_r2:
        url = await _upload_r2(out, safe_task, params)
        try:
            if out != src:
                out.unlink(missing_ok=True)
            src.unlink(missing_ok=True)
        except OSError:
            pass
        return None, url
    return out, None


async def _upload_r2(path: Path, safe_task: str, params) -> str:
    """S3-compatible upload to Cloudflare R2. Credentials live in env on the
    bridge; the plugin only sends bucket/prefix/public-base overrides."""
    acct = os.environ.get("NLM_R2_ACCOUNT_ID", "").strip()
    key_id = os.environ.get("NLM_R2_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("NLM_R2_SECRET_ACCESS_KEY", "").strip()
    bucket = (params.get("r2_bucket") or os.environ.get("NLM_R2_BUCKET") or "").strip()
    public_base = (params.get("r2_public_base")
                   or os.environ.get("NLM_R2_PUBLIC_BASE") or "").strip().rstrip("/")
    if not (acct and key_id and secret and bucket and public_base):
        raise _ProcessError(
            "R2 upload requested but NLM_R2_ACCOUNT_ID/ACCESS_KEY_ID/"
            "SECRET_ACCESS_KEY/BUCKET/PUBLIC_BASE are not all set")
    prefix = (params.get("r2_prefix")
              or os.environ.get("NLM_R2_PREFIX") or "").strip().strip("/")
    key = f"{prefix}/{safe_task}.mp4" if prefix else f"{safe_task}.mp4"

    def _do() -> None:
        import boto3
        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{acct}.r2.cloudflarestorage.com",
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            region_name="auto")
        s3.upload_file(
            str(path), bucket, key,
            ExtraArgs={
                "ContentType": "video/mp4",
                "CacheControl": "public, max-age=31536000, immutable"})

    try:
        await asyncio.to_thread(_do)
    except Exception as e:
        raise _ProcessError(f"R2 upload: {type(e).__name__}: {e}")
    return f"{public_base}/{key}"


@app.get("/artifact/download")
async def artifact_download(request: Request):
    """?task_id=&kind=audio|quiz|video&profile= — audio/video return the
    binary file, quiz returns parsed JSON. For kind=video the optional
    params trim_start/trim_end (seconds), watermark (image URL), wm_pos,
    wm_width (%), wm_margin (px) post-process via ffmpeg, and r2=1 (+
    r2_bucket/r2_prefix/r2_public_base overrides) uploads to Cloudflare R2
    and returns {"status":"success","url":...} instead of the binary."""
    if not _authed(request):
        return _err(401, "unauthorized", "invalid bearer token")
    task_id = request.query_params.get("task_id") or ""
    kind = (request.query_params.get("kind") or "audio").lower()
    profile = request.query_params.get("profile") or "p1"
    if not task_id or kind not in _ARTIFACT_KINDS:
        return _err(400, "bad_request",
                    "task_id + kind(audio|quiz|video) required")
    if MOCK:
        if kind == "quiz":
            return {"status": "success", "quiz": [
                {"question": "Mock question?", "options": ["A", "B"],
                 "answer_index": 0, "explanation": "mock"}]}
        return _err(400, "bad_request", "media download not mocked")

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
                if kind == "video":
                    out = _ARTIFACT_DIR / f"{safe_task}.mp4"
                    await client.artifacts.download_video(
                        nb_id, str(out), artifact_id=task_id)
                    # Fall through: ffmpeg trim/watermark + R2 upload run
                    # after the NotebookLM client session closes below.
                else:
                    out = _ARTIFACT_DIR / f"{safe_task}.json"
                    await client.artifacts.download_quiz(
                        nb_id, str(out), artifact_id=task_id,
                        output_format="json")
                    try:
                        data = json.loads(out.read_text(encoding="utf-8"))
                    except Exception:
                        data = {"raw": out.read_text(
                            encoding="utf-8", errors="replace")}
                    return {"status": "success", "quiz": data}
        except FileNotFoundError:
            return _err(502, "auth_missing", "storage_state.json not found")
        except Exception as e:
            return _err(502, "upstream_error", _err_cause(e))
    # kind == "video" reaches here with `out` holding the raw MP4.
    try:
        final_path, public_url = await _process_video(
            out, safe_task, request.query_params)
    except _ProcessError as e:
        return _err(502, "video_process_failed", str(e))
    except FileNotFoundError:
        return _err(502, "video_process_failed",
                    "ffmpeg/ffprobe not installed on the bridge")
    if public_url:
        return {"status": "success", "url": public_url,
                "processed": final_path is None}
    return FileResponse(str(final_path), media_type="video/mp4",
                        filename=f"{safe_task}.mp4")


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
        return _err(502, "upstream_error", _err_cause(e))
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
