"""FácilPost API. Facebook publishes scheduled posts even while the VPS is offline."""
from base64 import urlsafe_b64encode
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
import httpx
from pydantic import BaseModel, Field

app = FastAPI(title="FácilPost", docs_url=None, redoc_url=None, openapi_url=None)
COOKIE = "__Host-facilpost"
FLOW = "__Host-facilpost-flow"
TTL = 7 * 86400
MEDIA_LIMIT = 64 * 1024 * 1024
MIME = {"image/jpeg", "image/png", "image/webp", "video/mp4", "video/quicktime"}
FIELDS = ("id,message,created_time,scheduled_publish_time,permalink_url,"
          "full_picture,attachments{media_type,type,media,title}")
VERSION = os.getenv("META_GRAPH_VERSION", "v27.0")


def origin():
    return os.getenv("PUBLIC_ORIGIN", "").rstrip("/")


def config():
    app_id = os.getenv("META_APP_ID")
    secret = os.getenv("META_APP_SECRET")
    if not app_id or not secret or not origin().startswith("https://"):
        raise HTTPException(503, "Configure o aplicativo da Meta e o domínio HTTPS na VPS.")
    return app_id, secret


def cipher():
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"facilpost-page-tokens-v1").derive(config()[1].encode())
    return Fernet(urlsafe_b64encode(key))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@contextmanager
def database():
    directory = Path(os.getenv("STATE_DIR", "/var/lib/facilpost"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "state.db", timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS flows(state TEXT PRIMARY KEY, binding TEXT, expires INTEGER);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, selected TEXT, expires INTEGER);
            CREATE TABLE IF NOT EXISTS pages(
                session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                id TEXT NOT NULL, name TEXT NOT NULL, token BLOB NOT NULL,
                PRIMARY KEY(session,id));
        """)
        with connection:
            yield connection
    finally:
        connection.close()


@app.middleware("http")
async def private_response(request, next_handler):
    if request.method in {"POST", "PATCH", "DELETE"} and (
        not origin() or request.headers.get("origin") != origin()
    ):
        return JSONResponse({"detail": "Origem da requisição inválida."}, status_code=403)
    response = await next_handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def graph(method, path, token=None, *, params=None, data=None, files=None, video=False):
    host = "graph-video.facebook.com" if video else "graph.facebook.com"
    query = dict(params or {})
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
        query["appsecret_proof"] = hmac.new(config()[1].encode(), token.encode(), hashlib.sha256).hexdigest()
    try:
        res = httpx.request(method, f"https://{host}/{VERSION}/{path}", headers=headers,
                            params=query, data=data, files=files, timeout=240 if files else 25)
        payload = res.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Resposta da Meta indisponível. Atualize o calendário antes de repetir a ação.")
    if res.is_error or payload is False or (isinstance(payload, dict) and "error" in payload):
        problem = payload.get("error", {}) if isinstance(payload, dict) else {}
        code = problem.get("code")
        if code == 190:
            raise HTTPException(401, "A autorização expirou. Conecte sua Página novamente.")
        detail = problem.get("error_user_msg") or problem.get("message") or "Solicitação recusada pela Meta."
        if token:
            detail = detail.replace(token, "[oculto]")
        detail = detail.replace(config()[1], "[oculto]")
        raise HTTPException(429 if res.status_code == 429 or code in {4, 17, 613, 80001} else 400,
                            detail[:600])
    if payload is True:
        return {"success": True}
    if not isinstance(payload, dict):
        raise HTTPException(502, "Resposta inesperada da Meta.")
    return payload


def collection(path, token, **params):
    seen = set()
    params["limit"] = 100
    while True:
        payload = graph("GET", path, token, params=params)
        yield from payload["data"]
        paging = payload.get("paging", {})
        if not paging.get("next"):
            return
        # Preserve time-based as well as cursor-based paging. Never forward a
        # credential to a host selected by the upstream response.
        query = parse_qs(urlsplit(paging["next"]).query)
        markers = {k: vals[-1] for k, vals in query.items() if k in {"after", "until", "since", "__paging_token", "offset"}}
        if not markers and paging.get("cursors", {}).get("after"):
            markers["after"] = paging["cursors"]["after"]
        fingerprint = tuple(sorted(markers.items()))
        if not markers or fingerprint in seen:
            raise HTTPException(502, "A Meta não concluiu a paginação. Tente novamente.")
        seen.add(fingerprint)
        params.update(markers)


def session_id(request):
    return digest(request.cookies.get(COOKIE, ""))


def active_page(request):
    with database() as db:
        row = db.execute("""SELECT p.* FROM sessions s JOIN pages p ON
            p.session=s.id AND p.id=s.selected WHERE s.id=? AND s.expires>?""",
            (session_id(request), time.time())).fetchone()
    if not row:
        raise HTTPException(401, "Conecte uma Página do Facebook.")
    try:
        token = cipher().decrypt(row["token"]).decode()
    except InvalidToken:
        raise HTTPException(401, "Conecte sua Página novamente.")
    return row["id"], token


def cookie(response, name, value, age):
    response.set_cookie(name, value, max_age=age, secure=True, httponly=True, samesite="lax", path="/")


def remove_cookie(response, name):
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/session")
def session(request: Request):
    with database() as db:
        key = session_id(request)
        row = db.execute("SELECT selected FROM sessions WHERE id=? AND expires>?", (key, time.time())).fetchone()
        pages = db.execute("SELECT id,name FROM pages WHERE session=? ORDER BY name", (key,)).fetchall() if row else []
    return {"connected": bool(row), "pages": [dict(p) for p in pages], "page_id": row["selected"] if row else None}


@app.get("/api/facebook/connect")
def connect():
    try:
        app_id, _ = config()
    except HTTPException as error:
        return RedirectResponse("/?" + urlencode({"error": error.detail}), status_code=303)
    state, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with database() as db:
        now = time.time()
        db.execute("DELETE FROM flows WHERE expires<=?", (now,))
        db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
        db.execute("INSERT INTO flows VALUES(?,?,?)", (digest(state), digest(binding), now+600))
    options = {"client_id": app_id, "state": state, "response_type": "code",
               "redirect_uri": origin()+"/api/facebook/callback"}
    if login_config := os.getenv("META_LOGIN_CONFIG_ID"):
        options["config_id"] = login_config
    else:
        options["scope"] = "pages_show_list,pages_read_engagement,pages_manage_posts"
    query = urlencode(options)
    res = RedirectResponse(f"https://www.facebook.com/{VERSION}/dialog/oauth?{query}", status_code=303)
    cookie(res, FLOW, binding, 600)
    return res


@app.get("/api/facebook/callback")
def callback(request: Request, state: str = "", code: str = ""):
    with database() as db:
        consumed = db.execute("DELETE FROM flows WHERE state=? AND binding=? AND expires>?",
                               (digest(state), digest(request.cookies.get(FLOW,"")), time.time())).rowcount
    try:
        if not consumed or not code:
            raise HTTPException(400, "Conexão cancelada ou expirada. Tente novamente.")
        app_id, secret = config()
        exchanged = graph("GET", "oauth/access_token", params={"client_id":app_id, "client_secret":secret,
                            "redirect_uri":origin()+"/api/facebook/callback", "code":code})
        long_lived = graph("GET", "oauth/access_token", params={"grant_type":"fb_exchange_token",
                           "client_id":app_id, "client_secret":secret, "fb_exchange_token":exchanged["access_token"]})
        pages = [p for p in collection("me/accounts", long_lived["access_token"], fields="id,name,access_token,tasks")
                 if p.get("access_token") and set(p.get("tasks", [])) & {"CREATE_CONTENT","MANAGE","PROFILE_PLUS_CREATE_CONTENT","PROFILE_PLUS_MANAGE","PROFILE_PLUS_FULL_CONTROL"}]
        if not pages:
            raise HTTPException(400, "Nenhuma Página com permissão para publicar. Confira o acesso concedido no Facebook.")
        sid = secrets.token_urlsafe(32)
        key = digest(sid)
        vault = cipher()
        with database() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (session_id(request),))
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (key, pages[0]["id"], time.time()+TTL))
            db.executemany("INSERT INTO pages VALUES(?,?,?,?)",
                [(key,p["id"],p["name"],vault.encrypt(p["access_token"].encode())) for p in pages])
        res = RedirectResponse("/", status_code=303)
        cookie(res, COOKIE, sid, TTL)
    except HTTPException as exc:
        res = RedirectResponse("/?" + urlencode({"error":exc.detail}), status_code=303)
    remove_cookie(res, FLOW)
    return res


class PageSelection(BaseModel):
    id: str


@app.post("/api/page")
def select_page(request: Request, body: PageSelection):
    key = session_id(request)
    with database() as db:
        changed = db.execute("""UPDATE sessions SET selected=? WHERE id=? AND expires>?
            AND EXISTS(SELECT 1 FROM pages WHERE session=? AND id=?)""",
            (body.id,key,time.time(),key,body.id)).rowcount
    if not changed:
        raise HTTPException(403, "Página indisponível para esta sessão.")
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    with database() as db:
        db.execute("DELETE FROM sessions WHERE id=?", (session_id(request),))
    res = JSONResponse({"ok": True})
    remove_cookie(res, COOKIE)
    remove_cookie(res, FLOW)
    return res


def instant(value):
    try:
        date = datetime.fromisoformat(str(value).replace("Z","+00:00"))
        if date.utcoffset() is None:
            raise ValueError
        return int(date.timestamp())
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(422, "Informe data e hora com fuso horário.")


def schedule(value):
    stamp = instant(value)
    if not 600 <= stamp - time.time() <= 75 * 86400:
        raise HTTPException(422, "Escolha um horário entre 10 minutos e 75 dias a partir de agora.")
    return str(stamp)


def calendar_post(raw, status):
    at = raw.get("scheduled_publish_time") if status=="scheduled" else raw["created_time"]
    if at is None:
        return None
    stamp = int(at) if str(at).isdigit() else instant(at)
    att = next(iter(raw.get("attachments", {}).get("data", [])), {})
    media = att.get("media", {})
    kind = att.get("media_type") or att.get("type") or ""
    thumb = media.get("image", {}).get("src") or raw.get("full_picture")
    video = "video" in kind or bool(media.get("source"))
    return {"id":raw["id"], "message":raw.get("message") or att.get("title") or "",
            "at":datetime.fromtimestamp(stamp,timezone.utc).isoformat(), "status":status,
            "media_type":"video" if video else ("photo" if thumb else "text"),
            "thumbnail":thumb, "media_url":media.get("source") if video else thumb,
            "permalink":raw.get("permalink_url")}


@app.get("/api/posts")
def posts(request: Request, start: str, end: str):
    page, token = active_page(request)
    first, last = instant(start), instant(end)
    if not 0 < last-first <= 63 * 86400:
        raise HTTPException(422,"Período do calendário inválido.")
    found = {}
    for edge, status, filters in [
        ("scheduled_posts","scheduled",{}),
        ("published_posts","published",{"since":first,"until":last})]:
        for raw in collection(f"{page}/{edge}",token,fields=FIELDS,**filters):
            post = calendar_post(raw,status)
            if post and first <= instant(post["at"]) < last:
                found[post["id"]] = post
    return {"posts":sorted(found.values(),key=lambda p:p["at"])}


@app.post("/api/posts", status_code=201)
def create_post(request: Request, message: str = Form("", max_length=63206),
                at: str | None = Form(None), file: UploadFile | None = File(None)):
    page, token = active_page(request)
    message = message.strip()
    if not message and not file:
        raise HTTPException(422,"Escreva um texto ou selecione uma mídia.")
    params = {"published":"false","scheduled_publish_time":schedule(at)} if at else {}
    if not file:
        result = graph("POST", f"{page}/feed", token, data={**params, "message":message})
    else:
        if file.size is None or not 0 < file.size <= MEDIA_LIMIT or file.content_type not in MIME:
            raise HTTPException(422,"Selecione JPEG, PNG, WebP, MP4 ou MOV de até 64 MB.")
        video = file.content_type.startswith("video/")
        endpoint = "videos" if video else "photos"
        field = "description" if video else "message"
        upload = {"source":(file.filename or "midia",file.file,file.content_type)}
        result = graph("POST", f"{page}/{endpoint}", token, data={**params, field:message}, files=upload, video=video)
    return {"id":result.get("post_id") or result["id"]}


def post_token(request, post_id):
    page, token = active_page(request)
    if not re.fullmatch(re.escape(page) + r"_\d+", post_id):
        raise HTTPException(404,"Publicação indisponível nesta Página.")
    return token


class PostEdit(BaseModel):
    message: str = Field(max_length=63206)
    at: str | None = None


@app.patch("/api/posts/{post_id}")
def edit_post(request: Request, post_id: str, body: PostEdit):
    token = post_token(request,post_id)
    data = {"message":body.message}
    if body.at:
        data["scheduled_publish_time"] = schedule(body.at)
    graph("POST",post_id,token,data=data)
    return {"ok":True}


@app.delete("/api/posts/{post_id}")
def delete_post(request: Request, post_id: str):
    graph("DELETE",post_id,post_token(request,post_id))
    return {"ok":True}


@app.get("/api/posts/{post_id}/metrics")
def metrics(request: Request, post_id: str):
    payload = graph("GET",post_id,post_token(request,post_id),params={
        "fields":"reactions.limit(0).summary(true),comments.limit(0).summary(true),shares"})
    return {"reactions":payload.get("reactions",{}).get("summary",{}).get("total_count"),
            "comments":payload.get("comments",{}).get("summary",{}).get("total_count"),
            "shares":payload.get("shares",{}).get("count")}
