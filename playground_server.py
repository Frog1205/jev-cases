"""Atlas Playground local server: serves static files and proxies Jev / DeepSeek.

Keys and endpoints are read from environment variables only and never sent to the browser:
  TypeSafe_Key        required, Jev (TypeSafe) API key
  DEEPSEEK_API_KEY    optional, enables the DeepSeek comparison
  TYPESAFE_BASE_URL   default https://api.typesafe.ai
  DEEPSEEK_BASE_URL   default https://api.deepseek.com
  DEEPSEEK_MODEL      default deepseek-chat
  PLAYGROUND_PORT     default 8787
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("PLAYGROUND_PORT", "8787"))
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
MAX_BODY = 256 * 1024
TIMEOUT = 90
MAX_QUESTIONS = 8
QUESTION_TYPES = {"noul", "choice", "score"}
MODEL_RE = re.compile(r"^[\w.\-]{1,64}$")
QID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def endpoint(base, suffix):
    base = base.rstrip("/")
    return base if base.endswith(suffix) else base + suffix


def settings():
    return {
        "jev_key": env("TypeSafe_Key"),
        "jev_url": endpoint(env("TYPESAFE_BASE_URL", "https://api.typesafe.ai"), "/v1/systemone"),
        "llm_key": env("DEEPSEEK_API_KEY"),
        "llm_url": endpoint(env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), "/chat/completions"),
        "llm_model": env("DEEPSEEK_MODEL", "deepseek-chat"),
    }


class BadRequest(ValueError):
    pass


def validate(payload):
    if not isinstance(payload, dict):
        raise BadRequest("请求体必须是 JSON 对象")
    state = payload.get("state")
    if not isinstance(state, (str, dict, list)) or not state:
        raise BadRequest("state 不能为空")
    model = payload.get("model", "jev-latest")
    if not isinstance(model, str) or not MODEL_RE.match(model):
        raise BadRequest("model 格式不正确")
    questions = payload.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise BadRequest("至少需要一个问题")
    if len(questions) > MAX_QUESTIONS:
        raise BadRequest(f"最多 {MAX_QUESTIONS} 个问题")
    for qid, q in questions.items():
        if not QID_RE.match(qid):
            raise BadRequest(f"问题 ID「{qid}」只能包含字母、数字、下划线和连字符")
        if not isinstance(q, dict) or q.get("type") not in QUESTION_TYPES or not q.get("instructions"):
            raise BadRequest(f"问题「{qid}」缺少 type 或 instructions")
    return {"state": state, "model": model, "questions": questions}


PROXY_OPENER = urllib.request.build_opener()
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def uses_proxy(url):
    parts = urlsplit(url)
    return bool(urllib.request.getproxies().get(parts.scheme)) and not urllib.request.proxy_bypass(parts.hostname or "")


def make_request(url, data, headers):
    return urllib.request.Request(url, data=data, headers=headers, method="POST")


def open_url(url, data, headers):
    try:
        return PROXY_OPENER.open(make_request(url, data, headers), timeout=TIMEOUT)
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as err:
        # Handshake/connect failures mean the request never reached upstream, so a direct retry is safe.
        # ProxyHandler mutates the Request (set_proxy), so the retry needs a fresh one.
        if not uses_proxy(url) or isinstance(err.reason, TimeoutError):
            raise
        print(f"[proxy] {urlsplit(url).hostname} 经代理连接失败（{err.reason}），改为直连重试", file=sys.stderr)
        return DIRECT_OPENER.open(make_request(url, data, headers), timeout=TIMEOUT)


def post_json(url, key, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "atlas-playground/1.0",
    }
    start = time.perf_counter()
    try:
        with open_url(url, data, headers) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    latency = round((time.perf_counter() - start) * 1000)
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text) if text else None
    except ValueError:
        body = {"error": text[:2000]}
    return status, body, latency


def upstream_error(status, body, service):
    hints = {401: f"{service} 鉴权失败，请检查 API Key", 402: f"{service} 余额不足",
             422: "请求校验失败，请检查问题配置", 429: "请求过于频繁，请稍后重试",
             529: f"{service} 服务繁忙，请稍后重试"}
    detail = body.get("error") or body.get("detail") or body.get("message") if isinstance(body, dict) else body
    if isinstance(detail, dict):
        detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
    return {"error": hints.get(status, f"{service} 返回 HTTP {status}"), "status": status, "detail": detail}

def llm_prompt(data):
    lines = []
    for qid, q in data["questions"].items():
        t = q["type"]
        spec = {"id": qid, "type": t, "instructions": q["instructions"]}
        if q.get("criteria") is not None:
            spec["criteria"] = q["criteria"]
        if t == "noul":
            spec["answer_format"] = '"yes" 或 "no"'
        elif t == "choice":
            spec["answer_format"] = "criteria 中的一个选项名（原样返回键名）"
        else:
            spec["answer_format"] = f"0 到 {len(q.get('criteria') or []) - 1} 的整数等级"
        lines.append(json.dumps(spec, ensure_ascii=False))
    state = data["state"] if isinstance(data["state"], str) else json.dumps(data["state"], ensure_ascii=False)
    system = ("你是一个判断助手。阅读 state，逐个回答问题。只输出一个 json 对象，"
              '格式为 {"<问题id>": {"answer": <答案>, "reason": "<一句话理由>"}}，不要输出其他内容。')
    user = f"state:\n{state}\n\nquestions（每行一个）:\n" + "\n".join(lines)
    return system, user


def run_llm(data, cfg):
    system, user = llm_prompt(data)
    payload = {
        "model": cfg["llm_model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    status, body, latency = post_json(cfg["llm_url"], cfg["llm_key"], payload)
    if status != 200 or not isinstance(body, dict):
        return status, upstream_error(status, body or {}, "DeepSeek")
    content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    try:
        answers = json.loads(content)
    except ValueError:
        answers = None
    return 200, {"model": body.get("model"), "answers": answers, "text": content,
                 "usage": body.get("usage"), "latency_ms": latency}


class Handler(SimpleHTTPRequestHandler):
    server_version = "AtlasPlayground/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def host_ok(self):
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            self.send_error(421, "Misdirected Request")
            return False
        return True

    def send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.host_ok():
            return
        path = urlsplit(self.path).path
        if path == "/api/config":
            cfg = settings()
            return self.send_json(200, {
                "jev": {"configured": bool(cfg["jev_key"]), "endpoint": cfg["jev_url"]},
                "llm": {"configured": bool(cfg["llm_key"]), "endpoint": cfg["llm_url"], "model": cfg["llm_model"]},
            })
        if any(part.startswith(".") for part in path.split("/")) or path.endswith(".py"):
            return self.send_error(404)
        super().do_GET()

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self.host_ok():
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc not in ALLOWED_HOSTS:
            return self.send_json(403, {"error": "不允许跨站请求"})
        path = urlsplit(self.path).path
        if path not in ("/api/jev", "/api/llm"):
            return self.send_json(404, {"error": "未知接口"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self.send_json(413, {"error": "请求体为空或过大"})
        try:
            data = validate(json.loads(self.rfile.read(length).decode("utf-8")))
        except BadRequest as err:
            return self.send_json(400, {"error": str(err)})
        except ValueError:
            return self.send_json(400, {"error": "请求体不是合法 JSON"})

        cfg = settings()
        try:
            if path == "/api/jev":
                if not cfg["jev_key"]:
                    return self.send_json(503, {"error": "未配置环境变量 TypeSafe_Key"})
                status, body, latency = post_json(cfg["jev_url"], cfg["jev_key"], data)
                if status != 200 or not isinstance(body, dict):
                    return self.send_json(502, upstream_error(status, body or {}, "Jev"))
                body["latency_ms"] = latency
                return self.send_json(200, body)
            if not cfg["llm_key"]:
                return self.send_json(503, {"error": "未配置环境变量 DEEPSEEK_API_KEY"})
            status, body = run_llm(data, cfg)
            return self.send_json(200 if status == 200 else 502, body)
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            return self.send_json(504, {"error": "无法连接上游接口，请检查接口地址或网络", "detail": str(err)})


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    cfg = settings()
    print(f"Atlas Playground: http://{HOST}:{PORT}/playground.html")
    print(f"  Jev      {'已配置' if cfg['jev_key'] else '未配置 TypeSafe_Key'} -> {cfg['jev_url']}")
    print(f"  DeepSeek {'已配置' if cfg['llm_key'] else '未配置 DEEPSEEK_API_KEY'} -> {cfg['llm_url']} ({cfg['llm_model']})")
    server = ThreadingHTTPServer((HOST, PORT), partial(Handler, directory=str(ROOT)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
