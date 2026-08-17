import os
import sys
import json
import subprocess
import re
import time
import concurrent.futures
import requests
from datetime import datetime
import uuid
from flask import Flask, render_template, request, jsonify, g, after_this_request, Response

app = Flask(__name__)
# 开发期开启模板自动重载，避免修改 templates 后需手动重启 Flask 才生效
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


@app.before_request
def _before_request():
    """记录请求开始时间与原始请求体，供 after_request 写调用日志。"""
    g._req_start = time.time()
    try:
        g._req_body = request.get_data(as_text=True)[:4000]
    except Exception:
        g._req_body = ""


@app.after_request
def _after_request(response):
    """为所有 /api/* 请求写调用日志，包含时间、参数、响应状态、来源、耗时。"""
    try:
        if request.path.startswith("/api/"):
            duration_ms = (time.time() - g.get("_req_start", time.time())) * 1000
            _log_api_call(
                method=request.method,
                path=request.path,
                remote=request.remote_addr or "",
                ua=(request.user_agent.string if request.user_agent else "")[:120],
                body=g.get("_req_body", ""),
                status=response.status_code,
                duration_ms=duration_ms,
                extra={"redfox_configured": bool(_resolve_redfox_key())},
            )
    except Exception:
        pass
    return response


@app.before_request
def _require_access_auth():
    """公网暴露时（Cloudflare Tunnel）保护红狐/OpenAI 密钥与积分：所有请求需 Basic Auth。
    不豁免 127.0.0.1 —— 因为隧道程序是从本机连 localhost:8765，豁免会让公网流量免认证。"""
    auth = request.authorization
    if auth and auth.username == ACCESS_USER and auth.password == ACCESS_PASSWORD:
        return None
    return Response(
        "需要访问密码",
        401,
        {"WWW-Authenticate": 'Basic realm="NavalTool"'},
    )


# 项目根目录（所有相对路径以此为基准，保证拷贝到任意设备都能用）
BASE_DIR = os.environ.get("BASE_DIR") or os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
IDEA_BANK_PATH = os.path.join(DATA_DIR, "idea_bank.json")
API_CALL_LOG = os.path.join(DATA_DIR, "api_calls.log")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# 标题字数硬上限（含标点）：用于文章生成与首条评论的标题校验
# 小红书标题硬性要求：不超过 20 个字符（含标点）
TITLE_MAX_LEN = 20

# ===================== 多设备可移植：落盘配置 config.json =====================
def load_cfg_file():
    """读取项目内的 config.json（跨平台、跨重启生效）。失败返回空字典。"""
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                obj = json.load(f)
                if isinstance(obj, dict):
                    return obj
    except Exception:
        pass
    return {}


def save_cfg_file(cfg):
    """把配置写回 config.json（仅本地文件，不进任何环境变量注册表）。"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# 全局配置（启动即加载；web UI 修改后实时更新）
_CFG = load_cfg_file()

# ===================== 公网访问密码保护（Cloudflare Tunnel 等场景） =====================
def _ensure_access_creds():
    """确保 config.json 中有访问账号/密码；首次运行自动生成随机密码，避免公网被刷积分。"""
    import secrets
    changed = False
    if not _CFG.get("access_user"):
        _CFG["access_user"] = os.environ.get("ACCESS_USER") or "owner"
        changed = True
    if not _CFG.get("access_password"):
        _CFG["access_password"] = os.environ.get("ACCESS_PASSWORD") or secrets.token_urlsafe(10)
        changed = True
    if changed:
        save_cfg_file(_CFG)
    return _CFG.get("access_user"), _CFG.get("access_password")


ACCESS_USER, ACCESS_PASSWORD = _ensure_access_creds()

# ===================== 多设备可移植：Python 解释器动态解析 =====================
def _resolve_python():
    """优先用项目自带 venv，否则用当前解释器；保证红狐脚本在任意设备上都能找到带依赖的 python。"""
    candidates = [
        os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe"),  # Windows venv
        os.path.join(BASE_DIR, ".venv", "bin", "python"),          # POSIX venv
        os.path.join(BASE_DIR, "venv", "Scripts", "python.exe"),
        os.path.join(BASE_DIR, "venv", "bin", "python"),
        os.environ.get("PYTHON_EXE") or "",
        sys.executable,
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return sys.executable


PYTHON_EXE = _resolve_python()

# ===================== 多设备可移植：红狐技能脚本动态解析 =====================
# 优先用项目内打包的 redfox_skills/（随项目拷贝，零外部路径依赖）；
# 其次用 RED_SKILLS_DIR 环境变量指向的目录；最后回退到标准 ~/.workbuddy/skills 位置。
def _resolve_skill_file(filename):
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(BASE_DIR, "redfox_skills", filename),
        os.path.join(os.environ.get("RED_SKILLS_DIR", ""), filename) if os.environ.get("RED_SKILLS_DIR") else "",
        os.path.join(home, ".workbuddy", "skills", "xiaohongshu-write", "scripts", filename),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    # 找不到时返回打包目录路径（调用时会报错，便于排查），保证变量始终非空
    return os.path.join(BASE_DIR, "redfox_skills", filename)


FETCH_SCRIPT   = _resolve_skill_file("fetch_xhs_hot_articles.py")
COVERS_SCRIPT  = _resolve_skill_file("fetch_explosive_covers.py")
TRENDS_SCRIPT  = _resolve_skill_file("fetch_xhs_trends.py")
WEEKLY_SCRIPT  = _resolve_skill_file("xhs_weekly_fetcher.py")
RANK_SCRIPT    = _resolve_skill_file("fetch_rank.py")
SIMILAR_SCRIPT = _resolve_skill_file("xiaohongshu_account_recommender.py")

# ===================== 红狐 API Key 解析（根因修复） =====================
# 问题：Git Bash 启动的 Flask 进程继承不到 Windows 用户级 REDFOX_API_KEY，
# 导致 RUNTIME 里是空 key / 旧 key，所有红狐调用鉴权失败、积分从不消耗。
# 解决：始终以"Windows 用户级环境变量 HKCU\Environment"为权威来源，用 winreg
# 直接读注册表（不依赖 powershell 子进程，沙箱不会拦截，任何启动方式都稳定）。
# 进程 env 与 /api/config/update 热更新作为优先覆盖（仅当用户显式提供时）。

def _read_win_user_env(name):
    """用 winreg 直接读 HKCU\\Environment（用户级环境变量），无需 powershell 子进程。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as hk:
            v = winreg.QueryValueEx(hk, name)[0]
            if v and str(v).strip():
                return str(v).strip()
    except Exception:
        pass
    return ""


def _win_user_redfox_key():
    """权威红狐 Key：优先 winreg 读用户级环境变量（稳定），powershell 仅作兜底。"""
    k = _read_win_user_env("REDFOX_API_KEY")
    if k:
        return k
    # 兜底：极少数环境 winreg 不可用时再试 powershell
    try:
        import subprocess
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Environment]::GetEnvironmentVariable('REDFOX_API_KEY','User')"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            k = (r.stdout or "").strip()
            if k:
                return k
    except Exception:
        pass
    return ""


def _win_user_env_var(name):
    """从 Windows 用户级环境变量读取任意变量（与红狐 Key 同源，winreg 优先）。"""
    v = _read_win_user_env(name)
    if v:
        return v
    try:
        import subprocess
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Environment]::GetEnvironmentVariable('%s','User')" % name],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            v = (r.stdout or "").strip()
            if v:
                return v
    except Exception:
        pass
    return ""


def _load_redfox_key():
    """启动时解析红狐 Key：进程 env > config.json > Windows 用户级(仅 Windows)。"""
    k = (os.environ.get("REDFOX_API_KEY") or "").strip()
    if k:
        return k
    k = (_CFG.get("redfox_api_key") or "").strip()
    if k and "..." not in k and "*****" not in k:
        return k
    return _win_user_redfox_key()


def _load_openai_key():
    """启动时解析 OpenAI Key：进程 env > config.json > Windows 用户级(仅 Windows)。"""
    k = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if k:
        return k
    k = (_CFG.get("openai_key") or "").strip()
    if k and "..." not in k and "*****" not in k:
        return k
    return _win_user_env_var("OPENAI_API_KEY")


def _resolve_redfox_key(redfox_api_key=None):
    """运行期解析红狐 Key，跨平台一致优先级：
    显式参数(拒绝脱敏) > 进程 env > config.json(落盘配置) > Windows 用户级(winreg，仅 Win) > RUNTIME 兜底。

    config.json 让配置在任意设备、任意重启后都生效，不再依赖 Windows 注册表；
    在 Windows 本机若 config.json 为空，仍回退到原 winreg 权威来源，行为与旧版一致。
    """
    if redfox_api_key and not _is_masked_key(redfox_api_key):
        return redfox_api_key.strip()
    k = (os.environ.get("REDFOX_API_KEY") or "").strip()
    if k:
        return k
    k = (_CFG.get("redfox_api_key") or "").strip()
    if k and not _is_masked_key(k):
        return k
    k = _win_user_redfox_key()  # 非 Windows 返回 ""
    if k:
        return k
    k = (RUNTIME.get("redfox_api_key") or "").strip()
    if k and not _is_masked_key(k):
        return k
    return ""


# 运行时配置（可被前端 /api/config/update 热更新）
RUNTIME = {
    "redfox_api_key": _load_redfox_key(),
    "openai_base": os.environ.get("OPENAI_API_BASE", "https://work.xclawxx.top/v1").rstrip("/"),
    "openai_key": _load_openai_key(),
    "default_model": os.environ.get("DEFAULT_MODEL", "Auto"),
}


def clean_content_symbols(text):
    """清理生成内容中不适合直接发布到小红书的 Markdown/装饰符号。
    保留纳瓦尔格式要求的结构符号（如 ## 分页、**加粗**），去掉引用符、项目符号、代码块等杂乱标记。
    """
    if not text:
        return text
    # 1) 去掉整行的 markdown 引用符 > 及其后可选空格
    text = re.sub(r"(?m)^>\s?", "", text)
    # 2) 去掉 ■ 装饰符（常见于小标题前）
    text = re.sub(r"■\s?", "", text)
    # 3) 去掉 markdown 代码块标记 ```
    text = re.sub(r"```\w*", "", text)
    # 4) 去掉独立的分隔线
    text = re.sub(r"(?m)^---\s*$", "", text)
    text = re.sub(r"(?m)^\*\*\*\s*$", "", text)
    # 5) 把 2 个及以上连续空行压缩为 1 个
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_xhs_tags(tags):
    """把 AI/模板返回的 tag 字符串统一解析成小红书可识别的话题格式。
    小红书可识别的格式：每个话题前后带 #，话题之间用空格分隔。
    支持输入：
      - 列表: ['#AI时代', '#收入管道']
      - 字符串(空格分隔): '#AI时代 #收入管道'
      - 字符串(无空格粘连): '#AI时代#收入管道#自我产品化'
      - 字符串(闭合话题): '#AI时代##收入管道#'
    输出: '#AI时代 #收入管道 #自我产品化'
    """
    if not tags:
        return "", []
    if isinstance(tags, (list, tuple)):
        raw = " ".join(str(t).strip() for t in tags if t)
    else:
        raw = str(tags).strip()
    # 1) 先把常见的非话题空格/换行统一；去掉头尾多余 # 簇拥
    raw = re.sub(r"#{2,}", "#", raw)
    # 2) 按小红书话题语法提取：#话题# 或 #话题
    found = re.findall(r"#[^#\s]+", raw)
    # 3) 去重并保持顺序
    seen = set()
    out = []
    for t in found:
        t = t.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return " ".join(out), out


def mask_key(k):
    """对 API Key 做简单脱敏展示"""
    if not k or len(k) < 12:
        return ""
    return k[:8] + "..." + k[-4:]


def _is_masked_key(k):
    """判断是否为脱敏展示用的 key（含 ... 或 *****），不能作为真实鉴权 key 使用。"""
    if not k:
        return False
    low = k.lower()
    return "..." in k or "*****" in low or low.count("*") >= 3


def _redact_secrets(text):
    """把日志里的 key/base 脱敏，避免泄露。"""
    if not text:
        return text
    # 红狐 ak_xxx / OpenAI sk-xxx / base url 里的域名
    text = re.sub(r"ak_[a-zA-Z0-9_-]{10,}", "ak_***REDACTED***", text)
    text = re.sub(r"sk-[a-zA-Z0-9]{10,}", "sk-***REDACTED***", text)
    text = re.sub(r"https?://[^\s\"'{}]+", "https://***REDACTED***", text)
    return text


def _log_api_call(method, path, remote, ua, body, status, duration_ms, extra=None):
    """把 API 调用记录追加到 data/api_calls.log。

    记录：时间、来源 IP、User-Agent、请求方法路径、请求体（脱敏）、响应状态、耗时、
         是否命中红狐、错误摘要。用于排查"测试环境有记录、生产环境无记录"类问题。
    """
    try:
        os.makedirs(os.path.dirname(API_CALL_LOG), exist_ok=True)
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "method": method,
            "path": path,
            "remote": remote,
            "ua": ua,
            "body": _redact_secrets(body),
            "status": status,
            "duration_ms": round(duration_ms, 2),
            "extra": extra or {},
        }
        with open(API_CALL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _log_redfox_call(skill_name, keyword, ok, error=None, result_summary=None):
    """单独记录一次红狐 skill 调用（便于和红狐后台 api_consume 对账）。"""
    try:
        os.makedirs(os.path.dirname(API_CALL_LOG), exist_ok=True)
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "type": "redfox",
            "skill": skill_name,
            "keyword": keyword,
            "ok": ok,
            "error": error,
            "result_summary": result_summary or {},
        }
        with open(API_CALL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def parse_multi_json(raw):
    dec = json.JSONDecoder()
    idx = 0
    objs = []
    while idx < len(raw):
        while idx < len(raw) and raw[idx] in " \n\r\t":
            idx += 1
        if idx >= len(raw):
            break
        try:
            obj, end = dec.raw_decode(raw, idx)
            objs.append(obj)
            idx = end
        except Exception:
            break
    return objs


def strip_code(raw):
    """去掉模型返回的 ```json ... ``` 包裹，返回纯 JSON 文本。"""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


def strip_trailing_tags(text):
    """去掉正文末尾连续的纯标签行（只含 #xxx 的标签，无正文）。

    用于保证：tags 只在『完整发布版 markdown』末尾出现一次，
    而图文内页（body / pages）不携带标签或提示词。
    """
    if not text:
        return text
    lines = text.split("\n")
    while lines and re.fullmatch(r"\s*(#\S+\s*)+", lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip()


def run_search(keyword, max_items=30, redfox_api_key=None):
    """调用 xiaohongshu-write 的 fetch_xhs_hot_articles.py 拉爆款笔记。带重试。"""
    env = os.environ.copy()
    env["REDFOX_API_KEY"] = _resolve_redfox_key(redfox_api_key)
    start_date = (datetime.now().replace(day=1)).strftime("%Y-%m-%d")
    cmd = [
        PYTHON_EXE,
        FETCH_SCRIPT,
        "--keyword", keyword,
        "--max-items", str(max_items),
        "--page-size", str(max_items),
        "--start-date", start_date,
        "--output-format", "json",
    ]
    last_err = {"error": "未知错误"}
    final_result = None
    for attempt in range(3):
        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            output = result.stdout
            if not output.strip():
                last_err = {"error": "接口无返回", "stderr": (result.stderr or "")[:300]}
                if attempt < 2:
                    time.sleep(1.5)
                    continue
                final_result = last_err
                break
            objs = parse_multi_json(output)
            if not objs:
                last_err = {"error": "无法解析返回", "raw": output[:500]}
                if attempt < 2:
                    time.sleep(1.5)
                    continue
                final_result = last_err
                break
            data = objs[0]
            if data.get("code") not in (0, None, "0") and data.get("code") is not None:
                final_result = {"error": data.get("msg", "接口错误"), "code": data.get("code")}
                break
            items = data.get("items", [])
            cleaned = []
            for it in items:
                cleaned.append({
                    "title": it.get("title", ""),
                    "authorNickname": it.get("authorNickname", ""),
                    "authorFans": it.get("authorFans", ""),
                    "likedCount": it.get("likedCount", 0),
                    "collectedCount": it.get("collectedCount", 0),
                    "commentsCount": it.get("commentsCount", 0),
                    "noteId": it.get("noteId", ""),
                    "coverUrl": it.get("coverUrl", ""),
                })
            final_result = {"items": cleaned, "total": data.get("total", len(cleaned))}
            break
        except subprocess.TimeoutExpired:
            last_err = {"error": "接口请求超时"}
            if attempt < 2:
                time.sleep(1.5)
                continue
            final_result = last_err
            break
        except Exception as e:
            last_err = {"error": str(e)}
            if attempt < 2:
                time.sleep(1.5)
                continue
            final_result = last_err
            break

    # 记录红狐调用结果，便于与红狐后台 api_consume 对账
    ok = bool(final_result and "items" in final_result and final_result.get("total", 0) > 0)
    err = (final_result or {}).get("error")
    _log_redfox_call(
        skill_name="search",
        keyword=keyword,
        ok=ok,
        error=err,
        result_summary={"total": final_result.get("total") if final_result else None, "has_items": "items" in final_result if final_result else False},
    )
    return final_result if final_result is not None else last_err


# ===================== 红狐多 skills 调度层 =====================

def _run_skill_script(script_path, args, redfox_api_key=None, timeout=90, retries=2):
    """通用执行 skill 脚本，返回 stdout 解析结果。失败返回 {'error': ...}。

    健壮性增强：
    - GBK/混合编码安全：用 errors='replace' 解码，避免小红书技能打印 GBK 字节导致
      UnicodeDecodeError 把真实 JSON 输出一并吞掉。
    - 自动重试：红狐接口偶发抖动时自愈，提升积分真正被消耗的成功率。
    """
    env = os.environ.copy()
    key = _resolve_redfox_key(redfox_api_key)
    if key:
        env["REDFOX_API_KEY"] = key
    cmd = [PYTHON_EXE, script_path] + args
    last_err = {"error": "未知错误"}
    skill_name = os.path.basename(script_path)
    final_result = None
    for attempt in range(retries + 1):
        try:
            result = subprocess.run(
                cmd, env=env, capture_output=True, timeout=timeout,
            )
            # GBK 安全解码（技能可能打印中文进度到 stdout）
            output = (result.stdout or b"").decode("utf-8", errors="replace").strip()
            if not output:
                last_err = {"error": "无输出", "stderr": (result.stderr or b"").decode("utf-8", errors="replace")[:300]}
            else:
                # 兼容 JSON 和 markdown 输出：优先解析 JSON
                objs = parse_multi_json(output)
                if objs:
                    final_result = objs[0]
                else:
                    final_result = {"markdown": output, "raw": output[:500]}
                break
        except subprocess.TimeoutExpired:
            last_err = {"error": "技能调用超时"}
        except Exception as e:
            last_err = {"error": str(e)}
        if attempt < retries:
            time.sleep(1.5)
    if final_result is None:
        final_result = last_err

    # 记录本次 skill 调用结果
    has_output = 'output' in locals() and bool(output)
    ok = has_output and not bool(final_result.get("error"))
    if isinstance(final_result, dict) and "items" in final_result:
        summary_type = "json"
    elif isinstance(final_result, dict) and "markdown" in final_result:
        summary_type = "markdown"
    elif ok:
        summary_type = "other"
    else:
        summary_type = "error"
    _log_redfox_call(
        skill_name=skill_name,
        keyword=" ".join(args)[:120],
        ok=ok,
        error=final_result.get("error"),
        result_summary={"type": summary_type},
    )
    return final_result


def run_covers(keyword, max_items=10, redfox_api_key=None):
    """xiaohongshu-cover：爆款封面数据。"""
    return _run_skill_script(
        COVERS_SCRIPT,
        ["--keyword", keyword, "--max-items", str(max_items), "--output-format", "json"],
        redfox_api_key, timeout=90,
    )


def run_trends(keyword, max_items=10, redfox_api_key=None):
    """xiaohongshu-title-score：爆款趋势数据。"""
    return _run_skill_script(
        TRENDS_SCRIPT,
        ["--keyword", keyword, "--max-items", str(max_items), "--output-format", "json"],
        redfox_api_key, timeout=90,
    )


def run_weekly(keyword, top_n=20, redfox_api_key=None):
    """xiaohongshu-weeklytop：七日热榜。"""
    return _run_skill_script(
        WEEKLY_SCRIPT,
        ["--keyword", keyword, "--top_n", str(top_n)],
        redfox_api_key, timeout=90,
    )


def run_rank(query, limit=10, redfox_api_key=None):
    """xiaohongshu-top-account：账号榜单。"""
    return _run_skill_script(
        RANK_SCRIPT,
        ["--query", query, "--limit", str(limit)],
        redfox_api_key, timeout=90,
    )


def run_similar_account(seed, redfox_api_key=None):
    """xiaohongshu-similar-account：对标账号（按赛道+粉丝数+等级）。"""
    # 默认按女性成长赛道 + 素人级别查询；用户可在后续扩展输入
    return _run_skill_script(
        SIMILAR_SCRIPT,
        ["--track", "学习教育", "--max_fans", "5000", "--level", "素人"],
        redfox_api_key, timeout=90,
    )


def gather_redfox_insights(topic, redfox_api_key=None):
    """并行聚合多个红狐 skill 的数据，用于丰富调研简报。"""
    import concurrent.futures
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        future_search = ex.submit(run_search, topic, 20, redfox_api_key)
        future_covers = ex.submit(run_covers, topic, 10, redfox_api_key)
        future_trends = ex.submit(run_trends, topic, 10, redfox_api_key)
        future_weekly = ex.submit(run_weekly, topic, 15, redfox_api_key)
        future_rank = ex.submit(run_rank, f"{topic} 日榜", 10, redfox_api_key)
        future_similar = ex.submit(run_similar_account, topic, redfox_api_key)

        out["search"] = future_search.result()
        out["covers"] = future_covers.result()
        out["trends"] = future_trends.result()
        out["weekly"] = future_weekly.result()
        out["rank"] = future_rank.result()
        out["similar"] = future_similar.result()
    return out


# ===================== 红狐 title-score / note-analyzer 技能封装 =====================

def _collect_titles_recursive(obj, out, limit):
    """递归收集任意结构里带 title/desc/noteTitle 的文案，去重收口至 limit。"""
    if len(out) >= limit:
        return
    if isinstance(obj, dict):
        for k in ("title", "desc", "noteTitle", "note_title", "displayTitle"):
            v = obj.get(k)
            if isinstance(v, str):
                t = " ".join(v.strip().replace("\n", " ").split())
                if t and t not in out:
                    out.append(t)
        for v in obj.values():
            _collect_titles_recursive(v, out, limit)
    elif isinstance(obj, list):
        for v in obj:
            _collect_titles_recursive(v, out, limit)


def _collect_refs(obj, out, limit):
    """从任意结构里抽取带 id/photoId/noteId 的参考爆款笔记。"""
    if len(out) >= limit:
        return
    if isinstance(obj, dict):
        pid = obj.get("photoId") or obj.get("id") or obj.get("noteId") or ""
        t = " ".join((obj.get("title") or obj.get("desc") or "").strip().replace("\n", " ").split())
        if pid and len(out) < limit:
            inter = (obj.get("interactiveCount") or obj.get("useLikeCount")
                     or obj.get("collectedCount") or obj.get("likedCount") or 0)
            out.append({"title": t, "link": f"https://www.xiaohongshu.com/explore/{pid}", "interactions": inter})
        for v in obj.values():
            _collect_refs(v, out, limit)
    elif isinstance(obj, list):
        for v in obj:
            _collect_refs(v, out, limit)


def gather_grounding(topic, redfox_api_key=None, max_titles=25, max_refs=3):
    """聚合并发拉取红狐爆款数据（趋势 + 搜索），返回真实标题/参考笔记，供 LLM 对标。

    趋势技能（title-score）偶发报错时，自动回退到稳定的搜索技能，
    保证 title-score / note-analyze 始终有真实爆款数据入账（且两端点都消耗红狐积分）。"""
    import concurrent.futures
    titles, refs, sources = [], [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        ft = ex.submit(run_trends, topic, 10, redfox_api_key)
        fs = ex.submit(run_search, topic, 20, redfox_api_key)
        trend = ft.result()
        search = fs.result()
    if isinstance(trend, dict) and not trend.get("error"):
        sources.append("trends")
        _collect_titles_recursive(trend, titles, max_titles)
        _collect_refs(trend, refs, max_refs)
    if isinstance(search, dict) and not search.get("error"):
        sources.append("search")
        _collect_titles_recursive(search, titles, max_titles)
        _collect_refs(search, refs, max_refs)
    # 去重标题
    seen, uniq = set(), []
    for t in titles:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return {"titles": uniq[:max_titles], "refs": refs[:max_refs], "sources": sources}


TITLE_SCORE_SYSTEM = """你是小红书爆款标题评分专家。基于下面提供的【真实爆款标题数据】（来自红狐接口，反映当下真实流量规律），对用户给出的标题做六维度加权评分。
六维度与权重：主题匹配度(15%)、结构合规度(20%)、利益清晰度(25%)、情绪唤醒度(20%)、稀缺性感知(15%)、合规安全性(5%)。
等级：S级(9.0+)/A级(7.0-8.9)/B级(5.0-6.9)/C级(<5.0)。
必须只返回一个 JSON 对象（不要 markdown、不要额外文字）：
{
  "total": 综合得分(0-10, 一位小数),
  "grade": "S/A/B/C",
  "dims": [{"name":"主题匹配度","weight":15,"score":0-10,"comment":"..."}, ...共6项],
  "summary": "一句话总评",
  "suggestions": ["优化建议1","建议2"],
  "alt_titles": ["5个更爆的替代标题，20字内，反常识/利益清晰/带小红书味"]
}
要求：评分必须基于真实爆款数据（标题结构/情绪/人群/痛点/利益），严禁凭空；alt_titles 要可落地。"""

NOTE_ANALYZE_SYSTEM = """你是小红书笔记优化助手。基于下面提供的【真实爆款数据】（红狐接口，提炼自全网爆款笔记），对用户文案做多维度评分。
四维度（各100分）：关键词覆盖、结构完整度、时效性、内容质量；总分取四维度平均(0-100)。
必须只返回一个 JSON 对象（不要 markdown、不要额外文字）：
{
  "total": 总分(0-100),
  "grade": "优秀/良好/一般/需改进",
  "dims": [{"name":"关键词覆盖","score":0-100,"comment":"覆盖/缺哪些热词"}, ...共4项],
  "suggestions": ["针对最低分维度的具体改法1","改法2","改法3"],
  "formula": "提炼的爆款公式，如 痛点开场+分点干货+互动收尾",
  "references": [{"title":"参考爆款标题","link":"https://www.xiaohongshu.com/explore/{photoId}","interactions":"收藏X/点赞X"} ...2-3条]
}
要求：评分必须基于真实爆款数据；references 必须来自真实数据；改进建议只针对正文内容。"""


def score_title_with_redfox(title, topic, api_key, base_url, model, redfox_api_key=None):
    """title-score 技能：拉真实爆款标题 + LLM 六维加权评分。"""
    g = gather_grounding(topic, redfox_api_key=redfox_api_key)
    titles, refs = g["titles"], g["refs"]
    titles_text = "\n".join("- " + t for t in titles) or "（无可用爆款数据）"
    user = f"""【待评分标题】{title}
【主题关键词】{topic}
【真实爆款标题数据（红狐接口，当下真实流量）】
{titles_text}
请基于以上真实爆款数据做六维度加权评分，并给出优化建议与更爆的替代标题。严格只返回 JSON。"""
    raw = call_openai_compatible(TITLE_SCORE_SYSTEM, user, api_key, base_url, model)
    raw = strip_code(raw)
    try:
        data = json.loads(raw)
    except Exception:
        data = {"error": "评分结果解析失败", "raw": raw[:600]}
    data["_redfox_ok"] = bool(g["sources"])
    data["_redfox_sources"] = g["sources"]
    data["_trend_count"] = len(titles)
    return data


def analyze_note_with_redfox(body, topic, api_key, base_url, model, redfox_api_key=None):
    """note-analyzer 技能：拉真实爆款 + LLM 四维度评分对标。"""
    g = gather_grounding(topic, redfox_api_key=redfox_api_key)
    titles, refs = g["titles"], g["refs"]
    titles_text = "\n".join("- " + t for t in titles) or "（无可用爆款数据）"
    refs_text = "\n".join(f"- [{r['title']}]({r['link']})（互动：{r['interactions']}）" for r in refs) or "（无）"
    user = f"""【主题关键词】{topic}
【用户文案正文】
{body[:4000]}
【真实爆款数据（红狐接口，提炼自全网爆款笔记）】
爆款标题规律：
{titles_text}
参考爆款笔记：
{refs_text}
请基于真实爆款数据对用户文案做四维度评分对标，给出改进建议与爆款公式。严格只返回 JSON。"""
    raw = call_openai_compatible(NOTE_ANALYZE_SYSTEM, user, api_key, base_url, model)
    raw = strip_code(raw)
    try:
        data = json.loads(raw)
    except Exception:
        data = {"error": "分析结果解析失败", "raw": raw[:600]}
    data["_redfox_ok"] = bool(g["sources"])
    data["_redfox_sources"] = g["sources"]
    data["_trend_count"] = len(titles)
    return data


def gather_generation_inspiration(topic, redfox_api_key=None):
    """生成前拉真实爆款（search+trends）作为灵感喂给 AI，使每次生成都消耗红狐积分。

    用稳健的 gather_grounding（递归抽取任意结构标题 + trends/search 双源回退）。
    若传入的是长描述型选题（如含冒号的完整标题），先抽出短关键词检索，
    避免直接用长句检索导致红狐无结果、grounded 落空。"""
    kw = (topic or "").split("：")[0].split(":")[0].strip() or (topic or "")
    if len(kw) > 8:
        kw = kw[:8]
    g = gather_grounding(kw, redfox_api_key=redfox_api_key)
    if not g["titles"] and kw != (topic or ""):
        g = gather_grounding(topic, redfox_api_key=redfox_api_key)
    return g["titles"]


# ===================== 系统化内容资产库 =====================

VOICE_TEMPLATES = {
    "清醒陪伴型": {
        "开场称呼": "姐妹",
        "句式特点": "先共情再点破，像经历过的人轻轻拍醒你",
        "结尾": "你不是一个人，也不是没救。",
    },
    "反骨警示型": {
        "开场称呼": "朋友",
        "句式特点": "短句、反问、直接打脸，不给逃避留空间",
        "结尾": "醒醒。",
    },
    "温柔坚定型": {
        "开场称呼": "宝",
        "句式特点": "娓娓道来，语气软但立场硬，像姐姐给你兜底",
        "结尾": "慢慢来，我们一步一步来。",
    },
    "算账拆解型": {
        "开场称呼": "你",
        "句式特点": "量化、算账、给框架，把情绪翻译成数字",
        "结尾": "数字不会骗人。",
    },
    "故事共鸣型": {
        "开场称呼": "我有个朋友",
        "句式特点": "先讲一个真实故事，再引出道理，让人自己悟",
        "结尾": "她的故事，可能也是你的故事。",
    },
    "观点刺穿型": {
        "开场称呼": "说句扎心的",
        "句式特点": "一句话点破本质，金句密度高，不绕弯子",
        "结尾": "点醒一个是一个。",
    },
}

INSIGHT_ANGLES = {
    "反常识": "大多数人的认知是反的，真正决定差距的不是努力，而是分配。",
    "女性专属": "这件事对女生的掠夺更隐蔽，社会早把陷阱包装成奖励。",
    "算账视角": "把抽象概念量化成时间和金钱，一算账就发现自己在给别人打工。",
    "亲身经历": "用真实故事替代说教，我以前也这样，直到我试了一个月。",
    "二元对立": "廉价多巴胺 vs 复利，你选一边，时间就会把你带到那一边。",
    "身份重构": "你不是懒，也不是不够努力，你是在被设计成某个样子。",
}

# 选题矩阵：纳瓦尔核心主题 × 女性成长场景
TOPIC_MATRIX = {
    "财富": ["职场女性财富观", "女生搞钱避坑", "副业从0到1", "30岁女性金钱焦虑", "消费主义反杀"],
    "判断力": ["女生决策陷阱", "选择大于努力", "情绪与决策", "30岁重要选择", "职场判断力"],
    "幸福": ["女性幸福定义权", "不被定义的快乐", "幸福不是奖励", "中年女性幸福", "日常小确幸"],
    "杠杆": ["女生杠杆思维", "一人公司杠杆", "时间杠杆", "关系杠杆", "AI杠杆"],
    "专长": ["女生个人专长", "把热爱变专长", "技能复利", "副业专长", "不可替代性"],
    "复利": ["女生复利意识", "小习惯大复利", "注意力复利", "人际关系复利", "知识复利"],
    "自由": ["女性财务自由", "时间自由", "地理自由", "精神自由", "自由职业"],
    "学习": ["女性终身学习", "知识管理", "有效学习", "学习杠杆", "自学能力"],
    "决策": ["女性重大决策", "职业选择", "婚姻决策", "投资决策", "人生转折"],
    "注意力": ["女性注意力管理", "睡前刷手机", "算法喂养", "注意力账单", "数字戒断"],
    "关系": ["女性人际关系", "社交减法", "高质量关系", "边界感", "情感依赖"],
    "健康": ["女性健康投资", "精力管理", "睡眠复利", "身体账户", "健康杠杆"],
    "时间": ["女性时间管理", "时间审计", "时间贫困", "日程设计", "时间主权"],
    "产品化": ["把自己产品化", "个人品牌", "能力变现", "IP打造", "从技能到产品"],
}

# 纳瓦尔金句生成素材库（按主题）
# 不再用固定的 3 条轮询，而是把主题关键词代入多样化模板，配合 AI 生成，保证低重复、多角度。
QUOTE_THEME_KEYWORDS = {
    "财富": ["财富", "资产", "钱", "现金流"],
    "判断力": ["判断力", "决策", "认知", "选择"],
    "幸福": ["幸福", "满足", "快乐", "知足"],
    "杠杆": ["杠杆", "系统", "放大", "复利杠杆"],
    "专长": ["专长", "长板", "天赋", "不可替代性"],
    "复利": ["复利", "积累", "坚持", "长期主义"],
    "自由": ["自由", "选择权", "自律", "边界"],
    "学习": ["学习", "阅读", "成长", "元技能"],
    "决策": ["决策", "选择", "判断", "机会成本"],
    "注意力": ["注意力", "专注", "算法", "清醒"],
    "关系": ["关系", "人际", "边界", "长期关系"],
    "健康": ["健康", "精力", "身体", "能量"],
    "时间": ["时间", "日程", "生命", "时间主权"],
    "产品化": ["产品化", "IP", "作品", "可复制"],
}

# ===================== 纳瓦尔真实思想金句库 =====================
# 用于「纳瓦尔金句（文章核心钩子）」模块：必须从这里出，不能围绕用户选题瞎编。
# 每个主题 8-10 条，核心观点来自 Naval Ravikant，中文表达做了小红书化压缩。
NAVAL_QUOTES = {
    "财富": [
        "追求财富，而不是金钱或地位。",
        "财富是你睡觉时仍在赚钱的资产。",
        "不要用时间换钱，去拥有能产生财富的资产。",
        "穷人和富人的区别，是资产和负债的区别。",
        "金钱是社会转移时间与财富的信使。",
        "财富的正和游戏：创造价值，而不是抢夺价值。",
        "没有杠杆的人，只能用时间换钱。",
        "把自己产品化，是普通人创造财富的终极杠杆。",
    ],
    "判断力": [
        "如果难以抉择，答案就是否定的。",
        "智慧就是知道你的行为的长期后果。",
        "拒绝99%的机会，只对极少数说yes。",
        "好判断来自好输入、好模型和足够的耐心。",
        "情绪高涨时不答应，情绪低落时不拒绝。",
        "在不确定的世界里，判断力决定人生质量。",
        "多数人收集信息，少数人训练判断模型。",
        "清晰的判断，来自清晰的价值观。",
    ],
    "幸福": [
        "幸福是一种选择，也是一种技能。",
        "欲望是你跟自己的契约：直到得到它，你都不快乐。",
        "幸福的反面不是痛苦，而是麻木。",
        "真正的幸福，是内心没有必须拥有的东西。",
        "降低欲望，是比增加收入更快的幸福路径。",
        "满足不是躺平，而是幸福的复利。",
        "幸福的人，不是没有欲望，而是不被欲望劫持。",
        "把比较关掉，幸福就开始复利。",
    ],
    "杠杆": [
        "杠杆是一份努力被无限次复制的能力。",
        "代码、媒体、资本、劳动力，都是杠杆。",
        "没有杠杆的努力叫加班，有杠杆的努力叫积累。",
        "真正的杠杆，是脱离你依然能运转的系统。",
        "找到你的杠杆，让时间为你工作。",
        "杠杆时代，专长是燃料，产品是引擎。",
        " permissionless leverage（无许可杠杆）是普通人最大的礼物。",
        "用杠杆放大你的专长，而不是用努力填补差距。",
    ],
    "专长": [
        "专长无法被培训，只能被自我发现。",
        "你真正的专长，是那件事别人做起来像工作，你做起来像玩。",
        "市场不会为努力付费，只会为专长付费。",
        "别补短板了，把你的专长拉长到天际。",
        "真正值钱的专长，看起来都很小众。",
        "逃离竞争的唯一方法：做你自己。",
        "成为你自己，因为别人已经有人做了。",
        "专长加上杠杆，就是个人商业的核武器。",
    ],
    "自我产品化": [
        "把自己产品化，是你能创造的最大杠杆。",
        "专长加杠杆，就是产品化的自己。",
        "不要卖时间，卖产品。",
        "产品化的本质，是让能力脱离你也能运转。",
        "把你的经验产品化，它就变成了作品。",
        "产品化不是做网红，而是做可复用的资产。",
        "一份时间卖无数次，才是产品化的尽头。",
        "个人品牌，是可规模化的声誉。",
    ],
    "复利": [
        "所有回报都来自复利：财富、关系、知识。",
        "复利的第一条规则是：不要打断它。",
        "真正的复利，在最初90%的时间里都看不见。",
        "习惯、关系、认知，都在悄悄复利。",
        "每天1%的偏移，一年后是37倍的复利。",
        "复利只适合有耐心的人，因为它从不承诺即时反馈。",
        "相信复利，就是相信时间站在你这边。",
        "复利的起点越小，坚持的价值越大。",
    ],
    "自由": [
        "自由首先是不想要什么就能不要什么。",
        "自由是最大的财富。",
        "真正的自由，是拥有选择的权利和能力。",
        "自律即自由。",
        "自由不是没人管，而是没人管也自律。",
        "学会拒绝，是自由的第一课。",
        "自由不是想做什么就做什么，而是不想做就不做。",
        "退休不是年龄，而是不再为明天牺牲今天。",
    ],
    "学习": [
        "学习是一种元技能：一旦学会如何学习，你就能学会任何东西。",
        "阅读是最便宜的杠杆。",
        "读你爱读的，直到你爱上阅读。",
        "真正的知识来自实践，而不是课堂。",
        "学习的速度，取决于你输出的密度。",
        "输出倒逼输入，是学习最快的闭环。",
        "学习不是为了知道更多，而是为了判断更准。",
        "教，是最好的学习。",
    ],
    "决策": [
        "如果难以抉择，答案就是否定的。",
        "拒绝99%的机会，只对极少数说yes。",
        "决策的质量，决定你人生的版本。",
        "最差的决策，是把判断权交给别人。",
        "决策前问自己：这件事三年后还重要吗。",
        "不要因为害怕错过，而做出糟糕的决策。",
        "清晰的决策，来自清晰的价值观。",
        "慢决策，快执行。",
    ],
    "注意力": [
        "注意力是你最宝贵的资产。",
        "现代人不是在管理时间，而是在被算法管理注意力。",
        "专注是21世纪最稀缺的超能力。",
        "注意力花在哪，人生就在哪。",
        "保护注意力，就是保护你的人生质量。",
        "真正的注意力自由，是不被通知支配。",
        "注意力的贫穷，比金钱的贫穷更隐蔽。",
        "夺回注意力，是普通人最容易开始的逆袭。",
    ],
    "关系": [
        "选择与你共度时间的人，就是选择你的人生。",
        "你的身份，是你所交往的人的平均值。",
        "长期关系是复利最重要的形式之一。",
        "好的关系让你更自由，坏的关系让你更疲惫。",
        "关系的质量，取决于你敢不敢设边界。",
        "关系高手，都懂得先筛选，再经营。",
        "维护关系最好的方式：先把自己活好。",
        "远离有毒的人和环境，是人生最快的新陈代谢。",
    ],
    "健康": [
        "健康是一切的1，其他都是后面的0。",
        "你的身体是你唯一真正的资产。",
        "精力充沛才能做出好决策。",
        "健康不是目标，而是所有目标的底座。",
        "管理健康，比管理时间更重要。",
        "没有健康的自由，只是生病的自由。",
        "健康的状态，决定了你注意力的质量。",
        "身体是中年危机最好的护城河。",
    ],
    "时间": [
        "时间是你唯一不能借、不能赚、不能存的资产。",
        "你如何度过一天，就如何度过一生。",
        "时间管理的本质，是优先级管理。",
        "时间不是被填满的，而是被选择的。",
        "时间自由，是不再把时间卖给不喜欢的事。",
        "时间最大的浪费，是花在内耗上。",
        "时间主权，就是夺回安排自己一天的权力。",
        "时间的账，比钱的账更值得算。",
    ],
    "欲望": [
        "欲望是你跟自己的契约：直到得到它，你都不快乐。",
        "社会教你想要更多，幸福教你想要更少。",
        "满足不是放弃追求，而是停止被想要折磨。",
        "欲望的边界，就是你平静的边界。",
        "越少想要，越自由。",
        "你不是缺东西，你是被欲望骗了。",
        "控制欲望，是比控制时间更高级的自律。",
        "清空欲望，才能看见真正重要的事。",
    ],
}

# 用户选题 → 纳瓦尔主题的映射关键词（覆盖具体场景，如销售、播客、工作等）
NAVAL_TOPIC_KEYWORDS = {
    "财富": ["财富", "资产", "钱", "赚钱", "收入", "工资", "储蓄", "投资", "财务自由", "被动收入", "现金流", "富", "穷", "消费"],
    "判断力": ["判断力", "判断", "决策", "选择", "认知", "思维", "聪明", "智慧", "看透", "分析", "脑子"],
    "幸福": ["幸福", "快乐", "满足", "知足", "开心", "情绪", "心态", "焦虑", "内耗", "抑郁", "平静", "安宁"],
    "杠杆": ["杠杆", "放大", "效率", "系统", "自动化", "规模化", "工具", "团队", "雇佣", "流程"],
    "专长": ["专长", "擅长", "天赋", "优势", "长板", "独特", "不可替代", "技能", "专业", " competence", "销售"],
    "自我产品化": ["产品化", "个人品牌", "ip", "作品", "可复制", "内容", "自媒体", "影响力", "品牌"],
    "复利": ["复利", "积累", "坚持", "长期", "长期主义", "耐心", "沉淀", "雪球", "迭代", "滚雪球"],
    "自由": ["自由", "选择", "不想做", "拒绝", "fire", "退休", "裸辞", "逃离", "束缚"],
    "学习": ["学习", "读书", "阅读", "成长", "知识", "输入", "播客", "听课", "学", "看书", "耳朵", "信息"],
    "决策": ["决策", "决定", "选择困难", "取舍", "机会成本", "say no", "说不"],
    "注意力": ["注意力", "专注", "分心", "算法", "清醒", "刷手机", "短视频", "抖音", "通知", "干扰", "专注"],
    "关系": ["关系", "人际", "朋友", "圈子", "社交", "人脉", "交往", "相处", "亲密", "家人", "伴侣", "同事"],
    "健康": ["健康", "精力", "身体", "能量", "睡眠", "运动", "健身", "饮食", "休息", "累"],
    "时间": ["时间", "日程", "日程安排", "时间管理", "优先级", "规划", "拖延", "忙碌", "空"],
    "欲望": ["欲望", "想要", "需求", "攀比", "消费主义", "物质", "虚荣心", "占有欲", "买"],
}

def _match_topic_to_naval_themes(topic):
    """根据用户选题，匹配最相关的 1-3 个纳瓦尔主题。"""
    import re
    if not topic:
        return ["财富", "判断力", "幸福"]
    t = topic.lower()
    scores = {}
    for theme, kws in NAVAL_TOPIC_KEYWORDS.items():
        score = 0
        for kw in kws:
            if kw.lower() in t:
                score += 1
                # 更长的关键词匹配更有区分度
                score += min(len(kw) / 10, 0.5)
        if score:
            scores[theme] = score
    if not scores:
        return ["财富", "判断力", "幸福"]
    # 按得分排序，取前 3
    sorted_themes = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [theme for theme, _ in sorted_themes[:3]]

# 主题专属模板：每个主题 12-16 条，句式/用词刻意错开，避免雷同
QUOTE_PATTERNS = {
    "财富": [
        "追求{kw}，而不是金钱或地位。",
        "{kw}是你睡觉时也在赚钱的资产。",
        "穷人和富人的区别，是资产和负债的区别。",
        "真正的{kw}自由，是选择说不的自由。",
        "{kw}的本质，是用头脑赚钱，而不是用时间。",
        "不要出租你的时间，去拥有能产生{kw}的东西。",
        "收入的多少不重要，剩余多少才决定{kw}。",
        "{kw}的第一个杠杆，是让自己变得不可替代。",
        "想要{kw}，先把自己产品化。",
        "普通人用时间换钱，聪明人用{kw}系统换钱。",
        "你的{kw}安全垫，来自资产，而不是工资单。",
        "{kw}的尽头，是被动收入覆盖生活。",
        "会赚钱不难得，留住{kw}才难得。",
    ],
    "判断力": [
        "{kw}是杠杆时代最被低估的能力。",
        "在不确定的世界里，{kw}决定人生质量。",
        "智慧就是知道你的行为的长期后果。",
        "{kw}不是信息多，而是模型对。",
        "好{kw}来自好输入、好模型和足够的耐心。",
        "拒绝99%的机会，只对极少数说yes。",
        "如果难以抉择，答案就是否定的。",
        "{kw}的代价，往往要在三年后才会显现。",
        "多数人收集信息，少数人训练{kw}。",
        "{kw}的高手，都懂得为不确定性付费。",
        "错误的{kw}，比不努力更消耗人生。",
        "提升{kw}最快的方法：写下你为什么会错。",
    ],
    "幸福": [
        "{kw}是一种选择，也是一种技能。",
        "{kw}不是得到你想要的，而是学会想要你已经拥有的。",
        "欲望是你跟自己的契约：直到得到它，你都不快乐。",
        "{kw}的反面不是痛苦，而是麻木。",
        "真正的{kw}，是内心没有必须拥有的东西。",
        "{kw}不是被羡慕，而是睡得安稳。",
        "降低欲望，是比增加收入更快的{kw}路径。",
        "{kw}是一种计算能力：知道什么对自己真正重要。",
        "你不必拥有很多，也能拥有{kw}。",
        "{kw}的高手，都懂得主动关掉比较。",
        "知足不是躺平，而是{kw}的复利。",
        "{kw}的秘诀：把注意力从缺什么，移向有什么。",
    ],
    "杠杆": [
        "{kw}越大，责任越大，回报也越大。",
        "用代码、媒体、资本、劳动力{kw}你的专长。",
        "没有{kw}的人，只能用时间换钱。",
        "{kw}的本质，是一份努力被无限次复制。",
        "找到你的{kw}，让时间为你工作。",
        "{kw}让普通人也能撬动超额回报。",
        "真正的{kw}，是脱离你依然能运转的系统。",
        "{kw}不是借钱，而是让结果自动放大。",
        "把时间花在能{kw}的事情上。",
        "{kw}时代，专长是燃料，产品是引擎。",
        "没有{kw}的努力，叫加班；有{kw}的努力，叫积累。",
        "{kw}的边界，就是你的收入边界。",
    ],
    "专长": [
        "{kw}无法被培训，只能被自我发现。",
        "成为你自己，因为别人已经有人做了。",
        "你真正的{kw}，是那件事别人做起来像工作，你做来像玩。",
        "{kw}不是样样通，而是把一件事打透。",
        "找到自己的{kw}，然后把它产品化。",
        "{kw}的护城河，来自无法被复制的独特组合。",
        "市场不会为努力付费，只会为{kw}付费。",
        "{kw}的信号：别人觉得累，你觉得爽。",
        "别补短板了，把你的{kw}拉长到天际。",
        "{kw}不是天生的，是长期专注的结果。",
        "真正值钱的{kw}，看起来都很小众。",
        "{kw}加上杠杆，就是个人商业的核武器。",
    ],
    "复利": [
        "{kw}是世界第八大奇迹，懂的人赚它，不懂的人付它。",
        "所有回报都来自{kw}：财富、关系、知识。",
        "{kw}的第一条规则是：不要打断它。",
        "真正的{kw}，在最初90%的时间里都看不见。",
        "{kw}的敌人不是慢，而是频繁重启。",
        "每天1%的偏移，一年后是37倍的{kw}。",
        "{kw}只适合有耐心的人，因为它从不承诺即时反馈。",
        "习惯、关系、认知，都在悄悄{kw}。",
        "{kw}最可怕的地方：中断后要用两倍时间重启。",
        "相信{kw}，就是相信时间站在你这边。",
        "{kw}的起点越小，坚持的价值越大。",
        "{kw}不是奇迹，是纪律的别名。",
    ],
    "自由": [
        "{kw}首先是不想要什么就能不要什么。",
        "{kw}是最大的财富。",
        "自律即{kw}。",
        "没有收入来源的{kw}，叫恐慌。",
        "真正的{kw}，是拥有选择的权利和能力。",
        "{kw}不是没人管，而是没人管也自律。",
        "{kw}的底层，是离开任何收入都能活的底气。",
        "{kw}不是拥有的多，而是需要的少。",
        "学会拒绝，是{kw}的第一课。",
        "{kw}的代价，是承担选择的后果。",
        "{kw}不是想做什么就做什么，而是不想做就不做。",
        "{kw}的边界，由你的现金流和勇气共同决定。",
    ],
    "学习": [
        "{kw}是一种元技能，一旦你学会如何{kw}，你就能学会任何东西。",
        "阅读是最便宜的杠杆。",
        "真正的知识来自实践，而不是课堂。",
        "{kw}的速度，取决于你输出的密度。",
        "会{kw}的人，不是报课最多，而是犯错最多。",
        "{kw}的本质，是不断刷新自己的心智模型。",
        "输出倒逼输入，是{kw}最快的闭环。",
        "{kw}不是为了知道更多，而是为了判断更准。",
        "教，是最好的{kw}。",
        "{kw}的复利，来自持续不断的微小更新。",
        "不要收藏知识，要锻造{kw}系统。",
        "{kw}的终点不是博学，而是行动。",
    ],
    "决策": [
        "好{kw}来自好模型和好输入。",
        "拒绝99%的机会，只对极少数说yes。",
        "如果难以抉择，答案就是否定的。",
        "{kw}的质量，决定你人生的版本。",
        "最差的{kw}，是把判断权交给别人。",
        "{kw}前问自己：这件事三年后还重要吗。",
        "情绪高涨时不答应，情绪低落时不拒绝，是{kw}的底线。",
        "{kw}的高手，都懂得为不确定性预留空间。",
        "不要因为害怕错过，而做出糟糕的{kw}。",
        "{kw}的代价，往往是机会成本。",
        "清晰的{kw}，来自清晰的价值观。",
        "慢{kw}，快执行。",
    ],
    "注意力": [
        "{kw}是你最宝贵的资产。",
        "现代人不是在管理时间，而是在被算法管理{kw}。",
        "专注是21世纪最稀缺的超能力。",
        "{kw}花在哪，人生就在哪。",
        "{kw}是现代人唯一的货币。",
        "保护{kw}，就是保护你的人生质量。",
        "{kw}被收割的人，很难拥有自己的人生。",
        "真正的{kw}自由，是不被通知支配。",
        "{kw}的贫穷，比金钱的贫穷更隐蔽。",
        "夺回{kw}，是普通人最容易开始的逆袭。",
        "{kw}的质量，决定你思考的深度。",
        "{kw}不是被管理的，而是被选择的。",
    ],
    "关系": [
        "选择与你共度时间的人，就是选择你的人生。",
        "长期{kw}是复利最重要的形式之一。",
        "好的{kw}让你更自由，坏的{kw}让你更疲惫。",
        "{kw}的质量，取决于你敢不敢设边界。",
        "{kw}不是天天见面，而是见面时不消耗。",
        "低质量的{kw}，是对注意力的慢性盗窃。",
        "{kw}高手，都懂得先筛选，再经营。",
        "{kw}的本质，是价值的长期交换。",
        "你的{kw}，是你最真实的镜子。",
        "{kw}中的自由，来自敢于被讨厌。",
        "{kw}不是越多越好，而是越清醒越好。",
        "维护{kw}最好的方式：先把自己活好。",
    ],
    "健康": [
        "{kw}是一切的1，其他都是后面的0。",
        "你的身体是你唯一真正的资产。",
        "精力充沛才能做出好决策。",
        "{kw}不是目标，而是所有目标的底座。",
        "管理{kw}，比管理时间更重要。",
        "{kw}的复利，在身体垮掉之前看不见。",
        "真正的{kw}，是每天都能稳定输出精力。",
        "{kw}的投资回报率，远高于任何理财。",
        "{kw}不是自律，而是对自己的人生负责。",
        "没有{kw}的自由，只是生病的自由。",
        "{kw}是中年危机最好的护城河。",
        "{kw}的状态，决定了你注意力的质量。",
    ],
    "时间": [
        "{kw}是你唯一不能借、不能赚、不能存的资产。",
        "你如何度过一天，就如何度过一生。",
        "{kw}贫困比金钱贫困更隐蔽。",
        "{kw}管理的本质，是优先级管理。",
        "{kw}不是被填满的，而是被选择的。",
        "你每天的{kw}分配，就是你的价值观投票。",
        "{kw}自由，是不再把时间卖给不喜欢的事。",
        "{kw}最大的浪费，是花在内耗上。",
        "{kw}的复利，来自每天重复正确的小事。",
        "{kw}不等人，但可以被设计。",
        "{kw}主权，就是夺回安排自己一天的权力。",
        "{kw}的账，比钱的账更值得算。",
    ],
    "产品化": [
        "把自己{kw}，是你能创造的最大杠杆。",
        "专长 + 杠杆 = {kw}自我。",
        "不要卖时间，卖产品。",
        "{kw}的本质，是让能力脱离你也能运转。",
        "{kw}自己，就是把一份时间卖无数次。",
        "{kw}的尽头，是被动收入系统。",
        "{kw}不是做网红，而是做可复用的资产。",
        "{kw}能力，决定你收入的边界。",
        "把你的经验{kw}，它就变成了作品。",
        "{kw}的第一步：找到别人愿意付费的专长。",
        "{kw}不是加法，是乘法。",
        "{kw}的人，才能从时间换钱里毕业。",
    ],
}

# 通用金句模板：用于未命中主题或任意话题，保证总有输出
UNIVERSAL_QUOTE_PATTERNS = [
    "关于{topic}，最该被记住的不是答案，而是问题。",
    "{topic}不会背叛你，背叛你的是对它的错误使用。",
    "真正拉开差距的，不是努力，而是对{topic}的理解。",
    "大多数人都把{topic}当成消耗品，其实它是资产。",
    "你管理{topic}的方式，就是你管理人生的方式。",
    "{topic}本身不稀缺，清醒地使用{topic}才稀缺。",
    "如果{topic}不能被复利，你就不该长期投入。",
    "{topic}的高手，不是拥有更多，而是更懂拒绝。",
    "社会对{topic}的很多说法，其实是反的。",
    "{topic}的边界，就是你人生的边界。",
    "关于{topic}，最难的不是开始，而是持续。",
    "{topic}的自由，才是真的自由。",
    "别再把{topic}贱卖给最不需要它的人和事。",
    "{topic}不是目的，让你活得更像自己才是。",
]


def generate_local_quotes(theme, topic=None, count=6, used=None):
    """基于主题模板 + 通用模板，生成多样化、低重复的本地金句。
    会就地更新 used 集合（若传入），方便调用方持续去重。"""
    import random
    used = used if isinstance(used, set) else set(used or [])
    topic = (topic or theme or "这个主题").strip()

    pool = []
    # 主题专属模板 × 主题关键词，制造同主题不同表达的变体
    keywords = QUOTE_THEME_KEYWORDS.get(theme, [])
    patterns = QUOTE_PATTERNS.get(theme, [])
    for pat in patterns:
        if "{kw}" in pat:
            for kw in keywords:
                pool.append(pat.replace("{kw}", kw))
        else:
            pool.append(pat)

    # 通用模板 × 当前话题：仅当主题专属池子不够时再补充，
    # 避免把 "睡前刷手机" 这种具体场景硬塞进通用句式。
    if len(pool) < count:
        for pat in UNIVERSAL_QUOTE_PATTERNS:
            pool.append(pat.replace("{topic}", topic))

    # 兜底
    if not pool:
        pool = [
            f"关于{topic}，最该被记住的不是答案，而是问题。",
            f"{topic}本身不稀缺，清醒地运用它才稀缺。",
            f"真正拉开差距的，不是努力，而是对{topic}的理解。",
        ]

    random.shuffle(pool)
    results = []
    for q in pool:
        if q not in used:
            results.append(q)
            used.add(q)
        if len(results) >= count:
            break

    # 极端情况池子耗尽，用兜底句式继续生成（用主题关键词，而不是具体场景，避免 awkward）
    fallback_keyword = (QUOTE_THEME_KEYWORDS.get(theme, [topic]) or [topic])[0]
    fallback = [
        "关于{topic}，最该被记住的不是答案，而是问题。",
        "{topic}本身不稀缺，清醒地运用它才稀缺。",
        "真正拉开差距的，不是努力，而是对{topic}的理解。",
        "{topic}不会自动变好，只会被有意识地使用。",
        "你管理{topic}的方式，就是你管理人生的方式。",
    ]
    while len(results) < count:
        q = random.choice(fallback).replace("{topic}", fallback_keyword)
        if q not in used:
            results.append(q)
            used.add(q)
        else:
            # 避免死循环：在末尾追加序号
            results.append(q[:-1] + "。")
            break
    return results

# 真实切口模板库：自己的故事 / 身边人的故事 / 观察 / 实验 / 数据 / 对比
# 每个主题保留 15-20 条，配合随机/AI生成使用，避免 3 条轮询导致重复。
CUT_TEMPLATES = {
    "财富": [
        "我曾经以为理财是等有钱以后的事，直到我算了笔账，发现自己每年在'小钱'上流失了2万多",
        "我朋友月薪3万，但月底照样焦虑，因为她所有收入都绑在一份工作上",
        "我观察身边存下钱的女生，不是最会赚的，是最先给支出分类的",
        "我25岁时把'有钱'和'安全'划等号，30岁才发现现金流才是真正的安全感",
        "我曾花3个月工资买一个包，背了两次就压箱底，那笔钱现在够我撑过半年低谷",
        "我妈那代人觉得'省钱=抠门'，我这一代才懂：省下来的不是钱，是选择权",
        "我做过一个实验：连续30天记录每一笔支出，结果餐饮外卖占了我收入的四分之一",
        "我闺蜜工资只有我一半，三年后存款却比我多，因为她先学会了'延迟满足'",
        "我一度以为副业就是卖时间，后来才懂：真正能留下来的，是不靠我也能运转的资产",
        "我曾经看不起'小钱'，直到看到复利表：每天省50块，十年后差距是一辆车的首付",
        "我观察那些财务自由的女生，她们的第一桶金往往不是赚来的，是'不花'省出来的",
        "我曾为了面子接下很多不赚钱的合作，后来学会用'时薪'筛选一切机会",
        "我给自己定了一条规矩：任何超过月收入10%的消费，先放购物车冷静72小时",
        "我朋友每天记账5分钟，三年后她比我更清楚自己想要什么样的生活",
        "我曾经以为省钱会降低生活质量，现在发现：为真正重要的事花钱，才叫高质量生活",
    ],
    "判断力": [
        "我以前做重大决定前会反复问10个人，最后把自己绕晕",
        "我观察身边过得好的女生，往往不是最努力的，而是最会做选择的",
        "我25岁时因为害怕错过，接受了一份并不喜欢的高薪工作，3年后才知道那叫'机会成本'",
        "我曾以为信息越多决策越好，后来发现：淹死在信息里，比无知更可怕",
        "我闺蜜每次买房租房都要看50套，最后选的第一套还是最喜欢的",
        "我以前选工作只看工资，现在会先算：这份工作3年后能让我更值钱吗",
        "我做过最蠢的决定，大多发生在深夜和饥饿的时候",
        "我观察那些判断力强的女生，她们都有一套'反直觉清单'：情绪高时不答应、情绪低时不拒绝",
        "我曾用一个月做调研，结果错过了一个窗口期——完美主义有时候就是拖延的借口",
        "我现在遇到重大决定，会先问自己：如果是10年后的我，会怎么看这件事",
        "我以前特别相信'过来人'的经验，后来才发现：他们的天花板，差点成了我的天花板",
        "我同事永远在等'完全准备好'，另一个同事边做边调，三年后两人差距拉开了",
        "我发现最差的判断不是做错，而是把判断权交给了别人",
        "我给自己设了一个规则：任何让我'先答应再想想'的事，都先说不",
        "我曾为了短期收益放弃长期复利，那笔钱我现在还在后悔",
    ],
    "幸福": [
        "我曾经把幸福定义为'拥有更多'，结果拥有越多越慌",
        "我闺蜜离婚后才说，她最大的后悔是年轻时把幸福寄托在别人身上",
        "我发现自己最容易快乐的时刻，都不是买了什么，而是完成了一件小事",
        "我曾以为幸福是'被羡慕'，后来才发现：被羡慕和过得好，是两件事",
        "我给自己做过一个实验：每天睡前写下3件值得感恩的小事，一个月后焦虑少了一半",
        "我观察身边真正幸福的女生，她们不是没有问题，而是有问题也能睡个好觉",
        "我曾花很多钱买'治愈'，最后发现最有效的疗愈是关掉手机去散步",
        "我以前总觉得'等我有钱了/瘦了/升职了就幸福'，后来才懂幸福是一种日常练习",
        "我朋友每周末固定去公园发呆两小时，她说那是她一周最奢侈的消费",
        "我曾经以为幸福需要大事件，现在才发现：连续7天早睡带来的稳定情绪，就是幸福",
        "我35岁才明白：一个人能不能独处，决定了他能不能真正快乐",
        "我曾为了维持'人设'活得很累，直到我发现： nobody cares，除了我自己",
        "我妈总说'你要知足'，我以前觉得是鸡汤，现在发现知足是一种计算能力",
        "我做过最幸福的决定之一：把手机通知全关了24小时",
        "我发现幸福的反义词不是痛苦，而是麻木",
    ],
    "杠杆": [
        "我两年前还在用时间换钱，一小时100块；现在我写的东西还在帮我赚钱",
        "我朋友做设计，以前一份时间卖一次，现在一份模板卖1000次",
        "我观察普通女生和副业女生的差距：后者在找杠杆，前者在堆时间",
        "我曾经一天工作12小时还觉得不够，后来才懂：力气要用在能放大结果的地方",
        "我闺蜜把客户常问的问题整理成文档，现在她带团队省了一半重复劳动",
        "我以前觉得'借力'是麻烦别人，现在明白：能调动资源本身就是一种能力",
        "我做自媒体第三年才发现：真正值钱的不是内容，是可复用的内容系统",
        "我朋友会多国语言，她把翻译流程做成模板，学生遍布十几个国家",
        "我曾羡慕别人有团队，后来自己用AI+模板也做出了10倍产出",
        "我观察那些'看起来轻松'的女生，她们都在悄悄搭建自己的杠杆系统",
        "我以前认为努力就要亲力亲为，现在学会：能花小钱买时间的，绝不自己做",
        "我把一周的工作流程拆解后，发现80%的事情可以用SOP复用",
        "我朋友用一份课程大纲，换了三个平台、五种变现方式",
        "我曾经看不上'被动收入'，直到生病停工时才知道现金流有多重要",
        "我现在做任何项目前都会问：这件事做成功后，能不能不依赖我继续运转",
    ],
    "专长": [
        "我以前总羡慕别人会这个会那个，后来才发现我花了10年的那件事才是我的专长",
        "我同事什么都懂一点，却没有一样能让人付费",
        "我花了3年才真正承认，我擅长的那件事挺小，但足够深",
        "我曾经为了补短板累得要死，后来才发现：把长板做到极致，短板自然有人补",
        "我闺蜜做PPT做到全公司第一，后来这门'小技能'成了她的副业入口",
        "我曾以为专长是天生的，现在发现：它是无数个'再多做一点'堆出来的",
        "我观察那些在职场上不可替代的女生，她们往往不是最全面的，而是有一个'杀手锏'",
        "我以前害怕'定位太窄'，后来发现：窄才容易被记住，宽才容易被忽略",
        "我用一年时间只练一个技能，结果涨薪比过去三年还多",
        "我朋友会被问'这个问题只有你能解决吗'，她说那是一种很踏实的安全感",
        "我曾经把兴趣当专长，把专长当兴趣，结果两边都不突出",
        "我发现专长的信号是：别人做这件事会累，你做会进入心流",
        "我以前总想'跨界'，现在相信：先在一个点打透，再谈横向拓展",
        "我给自己定了一个规则：每年只深耕一个核心技能，其它随缘",
        "我观察真正赚钱的女生，她们往往不是最聪明的，而是把一件普通事练到极致的",
    ],
    "复利": [
        "我曾经坚持健身7天就想要马甲线，现在才明白复利是给有耐心的人准备的",
        "我连续写了100天复盘，回头看前面90天都像白费，第91天突然质变",
        "我朋友从25岁开始定投，35岁时同龄人还在焦虑，她已经可以不上班",
        "我曾以为复利只是理财概念，后来才懂：习惯、关系、认知都在复利",
        "我观察那些30岁后突然开挂的女生，大多在25岁时做了几件'当时看不到结果'的事",
        "我坚持每天读书30分钟，前半年没感觉，第18个月突然发现自己说话不一样了",
        "我以前总想'快速逆袭'，现在相信：真正改变人生的，是每天1%的偏移",
        "我做过一个实验：每天存20块，一年后我有了一笔'意外之财'",
        "我闺蜜每天雷打不动写500字，三年后她出了书，而我还停留在'想写'",
        "我曾经看不起小进步，直到算出：每天进步1%，一年后是37倍",
        "我发现复利最难的部分不是开始，是前90%的时间里看不到回报",
        "我以前容易'三天打鱼'，后来学会：把目标小到不可能失败，比如每天1个俯卧撑",
        "我观察身边状态稳定的人，她们都有一两个坚持超过三年的日常习惯",
        "我曾中断过一段好习惯，复健时发现：失去的进度要花两倍时间补回来",
        "我现在做任何长期计划，都先问自己：这件事我能持续做三年吗",
    ],
    "自由": [
        "我以前以为自由是有钱，后来才发现自由是可以说'不'",
        "我裸辞过一次，才明白没有收入来源的自由叫恐慌",
        "我朋友35岁才学会拒掉不喜欢的项目，她说这才叫真正成年",
        "我曾经把'自由'理解为没人管，现在发现：没人管也要自律，才是真自由",
        "我观察那些活得自由的女生，她们不是拥有的多，而是需要的少",
        "我曾为了'自由职业'辞职，结果比上班还焦虑，因为没有收入系统",
        "我现在对自由的定义是：有选择，并且有能力承担选择的代价",
        "我闺蜜每年只接6个客户，收入反而比满负荷时更高，因为质量上去了",
        "我以前觉得自由是'想做什么就做什么'，现在认为是'不想做什么就不做什么'",
        "我曾为了讨好所有人答应所有事，最后把自己锁死在别人的日程里",
        "我发现自由的底层不是存款数字，是'离开任何一个收入来源也能活'的能力",
        "我给自己设了一个'拒绝预算'：每周必须拒绝一个不符合方向的机会",
        "我朋友攒够了一笔'去你的基金'后，谈判都变得硬气了",
        "我曾经羡慕数字游民，后来发现：没有内在秩序的人，在哪都是囚徒",
        "我35岁才懂：真正的自由，是情绪不被外界随意牵动",
    ],
    "学习": [
        "我曾经一年读50本书，结果生活一点没变，因为我只是'收集'了知识",
        "我报过十几个网课，最后只坚持做了一件事：每天写500字",
        "我观察学得最快的女生，不是报课最多的，是输出最多的",
        "我曾以为学习就是'输入'，后来发现：能讲出来、做出来，才算学会",
        "我闺蜜每读完一本书都会写一张A4纸的践行清单，三年完全换了认知系统",
        "我以前追求'学完'，现在追求'用一次'——用过一次的知识才真正属于我",
        "我做过一个实验：用费曼技巧复述当天学到的东西， retention 提高了不止一倍",
        "我发现最有效的学习不是做笔记，是带着问题去搜索和实践",
        "我曾沉迷于'收藏=学会'的幻觉，现在收藏夹就是我的焦虑仓库",
        "我朋友学英语不用背单词App，而是每天看一集无字幕剧，半年后口语突飞猛进",
        "我以前总想要'体系化学习'，现在相信：从一个具体问题切入，效率最高",
        "我观察那些认知迭代快的女生，她们都有'教别人'的习惯",
        "我现在每学一个新概念，都会问：这和我的生活有什么关系",
        "我曾为了'全面发展'同时学三样东西，结果一样都没学会",
        "我发现学习最大的成本不是钱，是时间和注意力的沉没",
    ],
    "决策": [
        "我25岁时因为害怕错过，接受了一份并不喜欢的高薪工作，3年后才知道那叫'机会成本'",
        "我观察我妈那一代女性，很多重大决策都是被动做的",
        "我以前做选择只看眼前收益，现在会先问：这件事3年后还重要吗",
        "我曾以为决策高手是'想得清楚'，后来发现她们只是敢承担后果",
        "我闺蜜用一张A4纸把每个选项的'最好/最坏/最可能'写出来，选择变得很简单",
        "我以前做决策靠直觉，吃了几次亏后，学会用'可逆性'来筛选：不可逆的慢决策，可逆的快决策",
        "我发现最容易后悔的决定，大多是为了逃避短期痛苦",
        "我曾花三个月纠结要不要分手，后来明白：纠结本身，就是答案",
        "我观察那些决策干脆的女生，她们都有一套'默认值'：不清醒时不做重大决定",
        "我现在遇到两难选择，会先排除'会让我讨厌自己'的选项",
        "我朋友每次换城市前都会去住两周，她说：亲身体验比任何调研都准",
        "我曾经害怕选错，现在相信：大多数选择没有对错，关键是选完后怎么把它走对",
        "我给自己定了一条：晚上10点后不做任何重要决定",
        "我发现父母那代人很擅长'凑合'，而我们这一代要学会'不凑合'",
        "我曾为了安全感选了一个稳定但无成长的工作，五年后才敢重新选",
    ],
    "注意力": [
        "我曾经睡前刷3小时短视频，直到某天醒来眼睛疼到睁不开",
        "我朋友把智能手机换成功能机，一年后出了两本书",
        "我算了一笔注意力账，发现自己每天把最好的2小时交给了算法",
        "我曾以为'多任务处理'是效率，后来发现：切换一次任务，大脑要15分钟才能重新进入状态",
        "我闺蜜把手机调成黑白屏后，每天屏幕时间少了将近一半",
        "我以前一有空就刷手机，现在会随身携带一本小书，'无聊'时先翻书",
        "我做过一个实验：工作时把手机放在另一个房间，效率提升了40%",
        "我观察那些产出稳定的人，她们都主动设计了自己的'注意力环境'",
        "我曾以为休息就是刷手机，现在发现：刷手机是让大脑继续被投喂，不是休息",
        "我朋友每天早晨先写1000字再开手机，她说那是一天中唯一属于自己的时间",
        "我现在把App按颜色分组后发现：红色和橙色的App最容易被下意识点开",
        "我曾经因为'怕错过消息'不敢关通知，后来发现真正重要的事别人会打电话",
        "我给自己设了一个'数字安息日'：每周六下午不带手机出门",
        "我发现注意力最贵的不是时间，是它没有复利——被碎片信息切碎了，就很难再聚焦",
        "我以前总觉得自己没天赋，后来才承认：我只是把天赋浪费在了屏幕上",
    ],
    "关系": [
        "我曾经害怕拒绝别人，结果时间都被借走了",
        "我闺蜜结婚前把所有社交软件删了，专心做自己的项目",
        "我观察高质量关系的共同点：不是经常见面，是见面时不消耗",
        "我曾以为朋友越多越好，30岁后才懂：关系也需要断舍离",
        "我做过一个整理：列出最常联系的10个人，发现其中3个每次聊完都让我更累",
        "我以前不好意思谈钱，结果借出去的钱和人情都变成了内伤",
        "我观察那些边界感强的女生，反而更容易被珍惜",
        "我曾为了维持一段关系不断妥协，最后连自己是谁都模糊了",
        "我朋友每次答应别人前都会先问自己：这件事会让我 resentment 吗",
        "我现在相信：好的关系是互相充电，不是互相耗电",
        "我曾经把'秒回'当礼貌，后来发现：真正尊重你的人，不会因为你不秒回而离开",
        "我闺蜜有个原则：不借钱给没有还钱计划的人，她说这比面子重要",
        "我发现很多女生的人际关系焦虑，来自'想让所有人满意'",
        "我曾为了避免冲突而沉默，现在学会：温和但清晰地说出真实想法",
        "我现在每年都会主动疏远一两个人，不是因为恨，是因为我要保护我的能量",
    ],
    "健康": [
        "我曾经用咖啡续命3个月，结果一次体检吓到我重新安排生活",
        "我35岁才发现，精力管理比时间管理重要100倍",
        "我朋友每天只睡6小时还骄傲，直到她情绪彻底崩掉",
        "我曾以为年轻就是资本，现在知道：健康才是那个1，其他都是后面的0",
        "我观察身边状态好的女生，她们不一定健身最狠，但一定睡眠最好",
        "我给自己做过一个挑战：连续21天11点前睡，结果皮肤、情绪、效率全变了",
        "我以前把运动当减肥工具，现在把它当情绪排泄口",
        "我朋友戒糖三个月后说：原来'不清爽'不是天气，是血糖",
        "我发现最容易忽略的健康信号，是'总觉得累'",
        "我曾为了赶项目连续熬夜，后来发现：熬一次夜，后面三天都是低效补偿",
        "我现在把体检报告当成年度复盘，比年终总结还认真",
        "我闺蜜每周固定去大自然里走一次，她说那是她最低成本的能量补给",
        "我以前觉得健康是老年人的事，现在认为：管理精力就是管理人生",
        "我给自己定了一条：再忙也要先吃饭再干活",
        "我发现女生很多问题——焦虑、暴食、拖延——追根溯源都和睡眠不足有关",
    ],
    "时间": [
        "我曾经把日程排满到每一分钟，结果没有一分钟属于自己",
        "我算了一下，我每周花在无意义会议上的时间超过10小时",
        "我发现最忙的女生往往不是事情最多，而是不会拒绝",
        "我曾以为时间管理是把24小时用到极致，现在认为：时间管理的本质是价值排序",
        "我闺蜜每天只做3件最重要的事，她说其余都是噪音",
        "我以前喜欢列长长的待办清单，后来发现：清单越长，越焦虑",
        "我做过一个实验：记录一周时间花销，发现'刷手机'比我想象的多出3倍",
        "我观察那些从容的人，她们都懂得'留白'——日程里必须有空白",
        "我曾为了'高效'同时做两件事，结果两件事都做得稀烂",
        "我朋友每周五下午用来'无所事事'，她说那是创意的来源",
        "我现在判断一件事做不做，先看：它能不能由别人做，或者不做",
        "我以前觉得休息是浪费时间，现在发现：不休息才会浪费更多时间",
        "我给自己设了一个'时间审计'：月底看一下时间都花在哪了",
        "我发现真正的时间自由，不是有空，而是有选择把时间花在哪里的能力",
        "我曾为了省小钱花大量时间比价，后来学会用'时薪'衡量一切时间支出",
    ],
    "产品化": [
        "我以前只会埋头做事，后来发现同样的能力包装一下就能卖10倍",
        "我朋友把工作经验做成小册子，被动收入超过工资",
        "我观察能变现的女生，都问过自己：这件事能不能脱离我而存在",
        "我曾以为产品化是大公司的事，后来明白：一个人也能有产品",
        "我闺蜜把面试辅导流程做成录播课，一份时间卖了无数次",
        "我以前害怕'卖自己'，现在发现：把能力产品化，是对自己价值的尊重",
        "我做过一个练习：把我会的东西拆成'可交付单元'，才发现自己能卖的比想象多",
        "我观察那些副业做起来的女生，她们都先完成了'最小可用产品'",
        "我曾追求完美产品，结果迟迟不上线；后来学会：先卖再迭代",
        "我朋友用Notion模板月入过万，她说秘诀是'把重复咨询变成标准化交付'",
        "我现在做任何服务前都会想：这件事能不能变成模板、课程或工具",
        "我以前觉得'产品化'很冷，现在发现：好的产品是在帮用户省时间",
        "我给自己定了一个目标：每年至少把一个技能变成可复制的产品",
        "我发现产品化的最高境界，是客户购买时你正在睡觉",
        "我曾低估自己的小技能，后来有人愿意为我会的'普通事'付费，我才醒悟",
    ],
    "通用": [
        "我曾经在这一件事上栽过跟头，后来才明白纳瓦尔那句话不是鸡汤，是真的",
        "我朋友用一年的实验告诉我：最小的改变，也能拉开最大的差距",
        "我观察身边那些真正想通的女生，往往不是最努力的，而是最先想清楚的",
        "我曾经也在这个问题上反复横跳，直到做了一件很小但很具体的事，才稳下来",
        "我闺蜜跟我说过一句让我愣住的话，后来每次遇到类似情况都会想起",
        "我做过一个很笨但很有效的尝试：把这件事拆成每天能做的一个动作",
    ],
}

# AI 生成真实切口提示词：基于 Step 1 全部要素（选题、语气、角度、纳瓦尔金句、核心钩子金句），产出强相关的真实切口
CUT_GENERATION_SYSTEM = """你是一位擅长写小红书真实感内容的创作者。请根据用户给定的【选题/话题】【语气风格】【独到见解角度】【纳瓦尔金句】【核心钩子金句】，生成若干条"真实切口"（用于文章里第一人称的真实经历 / 观察）。

硬性要求：
1. **必须严格紧扣 Step 1 全部要素**：每条切口必须同时呼应【选题/话题】【语气风格】【独到见解角度】【纳瓦尔金句】【核心钩子金句】，体现同一套核心逻辑。绝不允许写与主题无关的内容（例如主题是"复利 / 系统思维 / 人生越活越顺"时，严禁写刷手机、注意力、短视频、算法这类无关切口）。
2. **切口要服务于核心钩子金句**：切口讲完后，读者能自然联想到上方的【核心钩子金句】，而不是跳到另一个话题。
3. **语气一致**：真实切口的语气要与上方给定的【语气风格】一致（如"清醒陪伴型"像闺蜜聊天，不爹味、不鸡汤）。
4. **角度一致**：切口的转折/顿悟点要呼应上方给定的【独到见解角度】（如"反常识"就要写出"表面合理、实际相反"的瞬间）。
5. **真实切口是第一人称或身边人视角的具体场景 / 故事 / 观察，30-60字**，有画面、有情绪、有转折，像闺蜜聊天一样自然。
6. 不要写道理、不要口号、不要堆概念词；要具体到一件小事、一个动作、一个瞬间。
7. 每条切口视角不同：可从"我自己 / 我朋友 / 我闺蜜 / 我同事 / 我观察 / 我做过一个实验 / 我算过一笔账"切入。
8. 情绪可多样：后怕、顿悟、尴尬、庆幸、懊悔、释然等。
9. 必须围绕【选题/话题】展开，让人一看就觉得"这说的就是我"。

只返回 JSON 数组，不要 markdown、不要额外解释：
["切口1","切口2","..."]"""

def generate_fresh_cuts(theme, scene, hot_title="", count=3, api_key=None, base_url=None, model=None, _used=None,
                        voice=None, insight=None, hook=None, core_quote=None, topic=None):
    """生成不重复的真实切口。优先用 AI（若 key 可用），否则从 CUT_TEMPLATES 随机抽取。
    _used 传入已用过的切口集合，避免本次生成重复。
    voice/insight/hook/core_quote/topic 用于让切口与 Step 1 的各要素严格匹配。"""
    import random
    used = set(_used or [])
    results = []

    actual_topic = (topic or theme or "").strip()
    actual_scene = (scene or actual_topic).strip()

    # 尝试 AI 生成（带全量 Step 1 上下文）
    if api_key and base_url and model:
        try:
            user_prompt = f"选题/话题：{actual_topic}"
            if actual_scene and actual_scene != actual_topic:
                user_prompt += f"\n场景：{actual_scene}"
            if hot_title:
                user_prompt += f"\n参考爆款标题：{hot_title}"
            if voice:
                user_prompt += f"\n语气风格：{voice}"
            if insight:
                user_prompt += f"\n独到见解角度：{insight}"
            if hook:
                user_prompt += f"\n纳瓦尔金句（文章核心钩子）：{hook}"
            if core_quote:
                user_prompt += f"\n核心钩子金句：{core_quote}"
            user_prompt += f"\n请生成 {count} 条真实切口："
            content = call_openai_compatible(CUT_GENERATION_SYSTEM, user_prompt, api_key, base_url, model, timeout=30)
            # 解析 JSON 数组
            m = re.search(r"\[[\s\S]*?\]", content)
            if m:
                arr = json.loads(m.group(0))
                if isinstance(arr, list):
                    for c in arr:
                        s = str(c).strip()
                        if s and s not in used and len(s) <= 120:
                            results.append(s)
                            used.add(s)
                        if len(results) >= count:
                            break
        except Exception:
            pass

    # Fallback / 补齐：从本地大库随机抽取，保证不重复。
    # 仅当原始 topic 明确命中 CUT_TEMPLATES 时才用主题池；
    # 若 _match_theme_to_hot_title 退回到默认"注意力"，则视为未命中，改用"通用"池，避免无关切口。
    theme_key = _match_theme_to_hot_title(actual_topic)
    if theme_key == "注意力" and "注意力" not in actual_topic:
        theme_key = None
    pool = CUT_TEMPLATES.get(theme_key, [])[:] if theme_key else []
    if not pool:
        pool = CUT_TEMPLATES.get("通用", [])[:]
    if not pool:
        pool = ["我曾经也在这个问题上摔过跟头，后来才明白纳瓦尔那句话不是鸡汤。"]
    random.shuffle(pool)
    for c in pool:
        if c not in used:
            results.append(c)
            used.add(c)
        if len(results) >= count:
            break

    # 兜底：如果该主题池子耗尽，用通用模板补齐
    fallback_pool = [
        "我曾经也在这个问题上摔过跟头，后来才明白纳瓦尔那句话不是鸡汤。",
        "我观察身边那些真正改变的女生，往往不是最努力的，而是最先想清楚的。",
        "我朋友用一年的实验告诉我： smallest 的改变，也能拉开最大的差距。",
    ]
    while len(results) < count:
        c = random.choice(fallback_pool)
        if c not in used:
            results.append(c)
            used.add(c)
        else:
            break
    return results


# ===================== 核心金句生成：多样化、低重复 =====================

CORE_QUOTE_GENERATION_SYSTEM = """你是一位纳瓦尔思想金句 curator（策展人）。

你的任务不是自由发挥，而是从给定的【纳瓦尔思想库】中，挑选并轻度润色出最适合作为小红书文章核心钩子的金句。

核心原则：
1. **必须根植于纳瓦尔思想**：每条金句的核心观点必须来自 Naval Ravikant 的真实思想（财富、判断力、幸福、自由、杠杆、专长、自我产品化、复利、阅读、人际关系、时间、欲望等），不能围绕用户的具体职业/场景（如销售、播客、上班）编造伪纳瓦尔句子。
2. **保留原意，只做小红书化表达**：可以把纳瓦尔的原话改得更短、更有钩子感、更适合女性成长/认知升级场景，但绝不能把原意改成"做销售后…""做播客…"这种个人场景叙事。
3. **紧扣用户选题主题**：从纳瓦尔思想库中选择【与用户选题主题最相关】的思想，例如用户谈"播客/阅读"就给阅读/判断力相关金句，谈"销售/工作"就给专长/杠杆/财富相关金句。
4. **多样化**：每条金句从不同纳瓦尔主题切入，避免重复。
5. **长度**：单条 15-40 字为宜，适合放在小红书封面当钩子。

只返回 JSON 数组，不要 markdown、不要额外解释。

示例输出：
["追求财富，而不是金钱或地位。","财富是你睡觉时仍在赚钱的资产。","不要用时间换钱，去拥有能产生财富的资产。"]"""


def generate_core_quotes(topic, theme=None, count=6, api_key=None, base_url=None, model=None, used=None):
    """生成「纳瓦尔金句（文章核心钩子）」。

    规则：
    1. 金句必须根植于纳瓦尔真实思想库 NAVAL_QUOTES，不能围绕用户的具体场景（销售/播客/上班）瞎编。
    2. 根据用户 topic 匹配最相关的 1-3 个纳瓦尔主题，从中挑选候选池。
    3. AI 仅做「挑选 + 轻度小红书化润色」，不能改变纳瓦尔原意，不能塞进用户场景。
    4. 无 AI 时直接返回本地纳瓦尔金句池的随机抽取。

    used 传入已用过的金句集合（或列表），会就地更新，方便调用方持续去重。
    """
    import random
    used = used if isinstance(used, set) else set(used or [])
    topic = (topic or "这个主题").strip()
    count = max(1, min(12, int(count)))
    results = []

    # 1) 根据用户选题匹配纳瓦尔主题，并构建候选金句池
    matched_themes = _match_topic_to_naval_themes(topic)
    candidate_pool = []
    for th in matched_themes:
        candidate_pool.extend(NAVAL_QUOTES.get(th, []))
    # 若匹配主题不足，补充通用高价值纳瓦尔金句
    if len(candidate_pool) < count * 2:
        for th in ["财富", "判断力", "幸福", "自由", "关系"]:
            if th not in matched_themes:
                candidate_pool.extend(NAVAL_QUOTES.get(th, []))
    # 去重
    seen = set()
    candidate_pool = [q for q in candidate_pool if not (q in seen or seen.add(q))]

    # 2) AI 仅做挑选 + 润色（不自由发挥）
    ai_ok = False
    if api_key and base_url and model and candidate_pool:
        try:
            # 只取前 24 条给 AI，避免提示词过长
            sample_pool = random.sample(candidate_pool, min(24, len(candidate_pool)))
            pool_text = "\n".join(f"{i+1}. {q}" for i, q in enumerate(sample_pool))
            user_prompt = (
                f"【用户选题】{topic}\n"
                f"【匹配到的纳瓦尔主题】{', '.join(matched_themes)}\n\n"
                f"【候选纳瓦尔金句池】\n{pool_text}\n\n"
                f"请从上面的纳瓦尔思想库中，挑选并轻度润色出 {count} 条最适合作为小红书文章核心钩子的金句。\n"
                f"要求：\n"
                f"1. 必须保留纳瓦尔原意，只能做表达压缩，不能改成用户的具体场景。\n"
                f"2. 优先选择与选题主题最相关的思想。\n"
                f"3. 每条 15-40 字，有钩子感，适合截图传播。\n"
                f"4. 只返回 JSON 数组，不要解释。"
            )
            content = call_openai_compatible(
                CORE_QUOTE_GENERATION_SYSTEM, user_prompt, api_key, base_url, model, timeout=40
            )
            m = re.search(r"\[[\s\S]*?\]", content)
            if m:
                arr = json.loads(m.group(0))
                if isinstance(arr, list):
                    for q in arr:
                        s = str(q).strip()
                        if s and s not in used and 10 <= len(s) <= 80:
                            results.append(s)
                            used.add(s)
                        if len(results) >= count:
                            break
                    ai_ok = len(results) >= count
        except Exception:
            pass

    # 3) AI 失败 / 无 AI：直接从候选池随机补齐
    if len(results) < count:
        random.shuffle(candidate_pool)
        for q in candidate_pool:
            if q not in used:
                results.append(q)
                used.add(q)
            if len(results) >= count:
                break

    # 4) 最终兜底：如果还是不够，从全部纳瓦尔金句里补
    if len(results) < count:
        all_quotes = []
        for qs in NAVAL_QUOTES.values():
            all_quotes.extend(qs)
        random.shuffle(all_quotes)
        for q in all_quotes:
            if q not in used:
                results.append(q)
                used.add(q)
            if len(results) >= count:
                break

    random.shuffle(results)
    return results


# 标签四层体系：身份 / 方法论 / 结果 / 平台热词
TAG_LAYERS = {
    "identity": ["#女性成长", "#女生必看", "#职场女性", "#30岁女生", "#独立女性", "#搞钱女孩", "#一人公司", "#副业女孩"],
    "method": ["#纳瓦尔", "#财富思维", "#认知升级", "#时间管理", "#注意力管理", "#复利思维", "#杠杆思维", "#产品化自己"],
    "outcome": ["#人间清醒", "#自我提升", "#财务自由", "#副业收入", "#精力管理", "#生活方式", "#自由职业", "#搞钱思路"],
    "trend": ["#女性力量", "#女性智慧", "#拒绝内耗", "#停止焦虑", "#girlstalk", "#女性成长智慧", "#大女主", "#清醒发言"],
}


def estimate_read_time(text):
    chars = len(text)
    minutes = max(1, round(chars / 400))
    return minutes


def generate_long_image_markdown(topic, angle, hook, voice, insight, cut, theme=None):
    vt = VOICE_TEMPLATES.get(voice, VOICE_TEMPLATES["清醒陪伴型"])
    ia = INSIGHT_ANGLES.get(insight, INSIGHT_ANGLES["反常识"])

    title_options = {
        "反常识": [
            f"真正拉开女生差距的，不是努力，是{topic}",
            f"穷女孩和富女孩，差的不只是钱：差的是{topic}",
        ],
        "女性专属": [
            f"女生最容易被偷走的东西，叫{topic}",
            f"为什么很多女生越努力越累？问题在{topic}",
        ],
        "算账视角": [
            f"算完这笔账，我再也不敢浪费{topic}",
            f"你每天浪费的{topic}，值多少钱？",
        ],
        "亲身经历": [
            f"我戒掉{topic}一个月后，生活全变了",
            f"说句丢人的，我曾经被{topic}控制了三年",
        ],
        "二元对立": [
            f"{topic}，你在选廉价多巴胺还是复利？",
            f"女生的{topic}，正在决定你十年后是谁",
        ],
        "身份重构": [
            f"你不是懒，你只是被设计成不会用{topic}",
            f"关于{topic}，我们从小被告知的都是错的",
        ],
    }
    title = title_options.get(insight, title_options["反常识"])[0]

    pain_openings = {
        "反常识": f"{vt['开场称呼']}，你有没有发现，每天下班后明明只想躺 10 分钟，结果一刷手机就到了凌晨？你不是在放松，你是在把仅剩的清醒时间，贱卖给一群年薪百万的算法工程师。",
        "女性专属": f"{vt['开场称呼']}，社会对女生的要求已经够多了：要美、要瘦、要情绪稳定、要会赚钱。可没人告诉你，有一群人正在偷偷收割你本可以用来变强的东西——{topic}。",
        "算账视角": f"{vt['开场称呼']}，来算笔账。如果你每天被{topic}偷走 2 小时，一年就是 730 小时。按 8 小时工作日算，相当于 91 个工作日。也就是说，你每年免费送别人 3 个月。",
        "亲身经历": f"{vt['开场称呼']}，说句实话，我曾经是那种睡前必刷 2 小时短视频的人。不是不想睡，是不敢睡——好像只有那点时间是属于我的。",
        "二元对立": f"{vt['开场称呼']}，{topic}这件事上，女生一直有两种选择：一种是即时反馈、轻松快乐；另一种是延迟满足、悄悄复利。大多数人无意识地选了第一种，然后困惑为什么自己一直原地打转。",
        "身份重构": f"{vt['开场称呼']}，你有没有想过，你的{topic}问题，可能根本不是因为你懒、不努力、不自律？而是这个社会早就把你训练成了一个不会管理它的人。",
    }
    pain = pain_openings.get(insight, pain_openings["反常识"])
    if cut:
        pain += f"\n\n我的真实经历是：{cut}"

    # 用真实纳瓦尔思想作为全文透镜；没给 hook 或 theme 时做兜底
    naval_thoughts = match_naval_thoughts(topic, hook=hook, theme=theme) if theme else []
    if not naval_thoughts and hook:
        naval_thoughts = [f"纳瓦尔说：『{hook}』"]
    if not naval_thoughts:
        naval_thoughts = [
            "纳瓦尔说：『注意力是你最宝贵的资产。』在算法时代，谁能守住注意力，谁就守住了自由。",
            "纳瓦尔说：『如果你拿不定主意，答案就是「不」。』对无关紧要的事说不，是保护注意力的武器。",
        ]
    thought_main = naval_thoughts[0]
    thought_secondary = naval_thoughts[1] if len(naval_thoughts) > 1 else naval_thoughts[0]

    core = f"{thought_main}\n\n但{vt['开场称呼']}，我今天想给你的不是鸡汤，是一个反常识的角度——{ia}\n\n大多数人以为自己在消费内容，其实是内容在消费你。你以为自己在休息，其实你的大脑正在被训练成『不会思考』。"

    # 让 section1 的小标题与正文也带上纳瓦尔主题，而不是完全通用
    section1_titles = {k: v.replace("注意力", topic) for k, v in {
        "反常识": f"■ 真正值钱的不是时间，是{topic}",
        "女性专属": f"■ 女生的{topic}，被切割得更碎",
        "算账视角": f"■ 你哪里是在放松，你是在逃避",
        "亲身经历": "■ 我试了一个月，发现戒断反应这么强",
        "二元对立": f"■ 廉价多巴胺，正在吃掉你的未来",
        "身份重构": f"■ 你不是懒，你是被设计成这样的",
    }.items()}
    section1_bodys = {k: v.replace("注意力", topic) for k, v in {
        "反常识": "穷人为什么穷？不是因为懒，是因为他们的注意力被廉价多巴胺切割成碎片。富人的日常也刷手机，但比例完全不同。他们大部分时间在输入、思考、输出、创造——每一件事都在把人生的秩序往前推一步。",
        "女性专属": "女生从小被训练 multitask：一边回工作消息一边刷小红书，一边做家务一边听播客。看起来高效，其实注意力早就被切碎了。真正值钱的能力，从来不是同时做很多事，而是长时间只做一件事。",
        "算账视角": "你选刷视频——免费、轻松、不用动脑。别人选写一篇文章、学一项技能——因为这一个小时的努力，可能在一年后还在产生复利。你选了什么不重要，重要的是你每天都在选同样的事。",
        "亲身经历": f"{cut or '我'}给自己定了一条规则：晚上 10 点后不碰短视频。第一周像戒毒，手会不自觉点那个 App。第二周开始能看进去书。第三周，我发现自己居然能早起了。",
        "二元对立": "廉价多巴胺是即时、易得、短暂的：刷一条视频爽 15 秒，放下就空虚。而复利多巴胺是延迟、费劲、持久的：读完一本书、写完一篇文章、跑完一次步，成就感会在你睡觉时继续发酵。",
        "身份重构": "你不是天生的'不会专注'。是你从小被允许边吃饭边看电视，边上课边刷手机，边工作边回消息。你的大脑被默认设置成了'碎片化'。想要改变，第一步不是自律，是承认这不是你的错，然后主动改设置。",
    }.items()}
    section1 = f"{section1_titles.get(insight, section1_titles['反常识'])}\n\n{section1_bodys.get(insight, section1_bodys['反常识'])}"

    section2_titles = {k: v.replace("注意力", topic) for k, v in {
        "反常识": "■ 清醒的人，看起来都很『无聊』",
        "女性专属": "■ 别把『对自己好』，过成『对自己狠』",
        "算账视角": "■ 复利不奖励偶尔努力，只奖励持续积累",
        "亲身经历": "■ 最可怕的是，你已经习惯被算法喂养",
        "二元对立": "■ 你选哪边，时间就站在哪边",
        "身份重构": "■ 改设置，而不是硬撑",
    }.items()}
    section2_bodys = {k: v.replace("注意力", topic) for k, v in {
        "反常识": "我认识一个朋友，两年出了五本书。她跟我说，秘诀简单到可笑：睡前手机放客厅，床头只放 Kindle 和纸笔。别人在追剧，她在写稿；别人在刷短视频，她在散步想事情。她的日子看起来很无聊，但她的人生在悄悄复利。",
        "女性专属": "『对自己好』不是买更多、刷更多、吃更多。真正对自己好，是把注意力收回来，花在能让你十年后感谢自己的事上。护肤、穿搭、恋爱技巧当然重要，但如果没有独立的判断力和持续成长，这些都会变成别人的生意。",
        "算账视角": "复利最残酷的地方在于：它不奖励偶尔的努力，只奖励持续的积累。而持续的积累，需要持续的专注。你这一天看了 50 页书不算什么，但你连续 300 天每天看 50 页，你就是另一个人。",
        "亲身经历": "纳瓦尔说，大多数人只是活在默认设置里。算法喂什么，他们就消费什么。最可怕的不是你不知道自己在浪费时间，而是你的手指比大脑更快做出反应——你已经被训练成不需要思考就能打开那个 App 的人。",
        "二元对立": "每天 2 小时，你可以用来刷，也可以用来写。短期看没差别，一年后一种是空虚+0 作品，另一种是完成了一部作品、一项技能、一个新的自己。时间对所有人都公平，它只认你日复一日的选择。",
        "身份重构": "不要一上来就挑战自己'能不能坚持 21 天'。先改环境设置：睡前手机放客厅、把娱乐 App 藏起来、给最重要的工作设定固定时间。人是环境的产物，把环境改成有利于你的，自律就会变得容易很多。",
    }.items()}
    section2 = f"{section2_titles.get(insight, section2_titles['反常识'])}\n\n{section2_bodys.get(insight, section2_bodys['反常识'])}"

    quote_section = f"{thought_secondary}\n\n对那条推送说不，对那个让你再刷一集的按钮说不，对『看完这个就去学习』的念头说不。这些『不』加起来，就是你和大多数人拉开差距的地方。"

    closings = {
        "清醒陪伴型": f"从今天开始，把{topic}当成你最值钱的资产——因为它本来就是。你把它投给谁，你的未来就属于谁。\n\n{vt['结尾']}",
        "反骨警示型": f"别再骗自己了。你不是没时间，你是把{topic}全给了算法。今晚就试一次：打开飞行模式，看看你能撑多久。\n\n{vt['结尾']}",
        "温柔坚定型": f"改变不用从明天开始，从今晚睡前把手机放远一点开始。你不是在和算法对抗，你是在把自己一点一点找回来。\n\n{vt['结尾']}",
        "算账拆解型": f"给你的{topic}做一次审计：过去 24 小时，它花在了哪里？这个数据，会告诉你一年后你在哪里。\n\n{vt['结尾']}",
        "故事共鸣型": f"我认识的那个女生，一开始也只是想少刷半小时。后来她发现，{topic}收回来以后，人生突然多了很多可能。\n\n{vt['结尾']}",
        "观点刺穿型": f"一句话：你的{topic}流向哪里，你的人生就流向哪里。别再把最值钱的东西，免费送给别人的算法。\n\n{vt['结尾']}",
    }
    closing = closings.get(voice, closings["清醒陪伴型"])

    tags = generate_tags(topic, insight)

    # 核心观点从正文里提取一段 40-80 字的精彩段落，而不是口号式短句
    full_text = "\n\n".join([pain, core, section1, section2, quote_section, closing])
    core_viewpoint = extract_core_viewpoint(full_text, fallback=f"真正拉开女生差距的，不是努力，是你把{topic}投给谁。")
    cover_subtitle = short_hook(core_viewpoint)

    word_count = len(full_text.replace(" ", "").replace("\n", ""))
    read_time = estimate_read_time(full_text)

    pages = [
        {"type": "cover", "title": title, "subtitle": cover_subtitle},
        {"type": "page", "title": "", "body": pain},
        {"type": "page", "title": "", "body": core},
        {"type": "page", "title": section1.split("\n\n")[0].replace("■ ", ""), "body": "\n\n".join(section1.split("\n\n")[1:])},
        {"type": "page", "title": section2.split("\n\n")[0].replace("■ ", ""), "body": "\n\n".join(section2.split("\n\n")[1:])},
        {"type": "page", "title": "", "body": quote_section + "\n\n" + closing},
    ]

    markdown = f"# {title}\n\n> 💡 核心观点：{core_viewpoint}\n\n"
    for p in pages[1:]:
        if p["title"]:
            markdown += f"## {p['title']}\n\n"
        markdown += f"{p['body']}\n\n"
    if tags:
        markdown += f"\n{tags}\n"

    detail_caption = build_detail_caption(markdown, topic, hook, voice, insight, theme=theme)

    return {
        "title": title,
        "subtitle": cover_subtitle,
        "word_count": word_count,
        "read_time": read_time,
        "pages": pages,
        "markdown": markdown,
        "tags": tags,
        "core_viewpoint": core_viewpoint,
        "detail_caption": detail_caption,
        "mode": "template",
    }


def generate_tags(topic, insight):
    """从四层标签体系里组合 8-10 个标签，保证覆盖所有层级。支持主题或具体场景。"""
    layers = TAG_LAYERS
    selected = []
    selected.extend(layers["identity"][:2])
    selected.extend(layers["method"][:2])
    selected.extend(layers["outcome"][:2])
    selected.extend(layers["trend"][:2])

    topic_tags = {
        "财富": ["#财富思维", "#搞钱女孩"],
        "判断力": ["#认知升级", "#决策力"],
        "幸福": ["#幸福感", "#情绪管理"],
        "杠杆": ["#杠杆思维", "#效率"],
        "专长": ["#个人成长", "#核心竞争力"],
        "复利": ["#复利思维", "#长期主义"],
        "自由": ["#财务自由", "#自由职业"],
        "学习": ["#学习方法", "#自我提升"],
        "决策": ["#决策力", "#人生选择"],
        "注意力": ["#注意力管理", "#数字戒断"],
        "关系": ["#人际关系", "#社交减法"],
        "健康": ["#精力管理", "#健康生活"],
        "时间": ["#时间管理", "#时间主权"],
        "产品化": ["#个人品牌", "#IP打造"],
    }

    # 先精确匹配主题
    matched = topic_tags.get(topic, [])
    if not matched:
        # 再按具体场景反查所属主题
        for theme, scenes in TOPIC_MATRIX.items():
            if topic in scenes or any(topic in s for s in scenes):
                matched = topic_tags.get(theme, [])
                break
    selected.extend(matched)
    return " ".join(selected[:10])


def smart_truncate(text, max_len=80, min_len=40):
    """在 max_len 附近找最后一个标点截断，保证语义完整；找不到则加省略号。"""
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    # 优先句号，其次逗号/分号/破折号
    last_punct = max(cut.rfind("。"), cut.rfind("，"), cut.rfind("；"), cut.rfind("——"))
    if last_punct >= min_len:
        return text[:last_punct + 1]
    # 退而求其次：空格或中文空格
    last_space = max(cut.rfind(" "), cut.rfind("　"))
    if last_space >= min_len // 2:
        return text[:last_space].strip() + "……"
    return cut + "……"


def extract_core_viewpoint(body, fallback="", min_len=40, max_len=80):
    """从正文 body 中提取一段 40-80 字的精彩、吸睛段落作为核心观点。

    策略：
    1. 优先提取被『』包裹的金句；若金句本身够长（>= min_len）直接返回；
       若金句较短，向后扩展到下一个完整句，使总长在 min_len~max_len 之间。
    2. 其次按句切分，选最长且含有冲突/洞见词（差距、真正、不是、偷走、复利、算法、选择等）的一句。
    3. 兜底取正文开头的前 max_len 字（语义截断）。
    """
    text = (body or "").strip()
    if not text:
        return fallback

    # 1. 优先金句块：提取『...』内部文本
    quote_match = re.search(r"『([^』]{10,120})』", text)
    if quote_match:
        inner = quote_match.group(1).strip()
        full_quote = f"『{inner}』"
        if min_len <= len(full_quote) <= max_len:
            return full_quote
        if len(full_quote) > max_len:
            return smart_truncate(full_quote, max_len, min_len)
        # 金句较短，向后扩展一个完整句
        after = text[quote_match.end():]
        # 找下一个完整句（句号/问号/感叹号）
        m = re.search(r".{0,5}[^。！？]*[。！？]", after)
        if m:
            extended = (text[quote_match.start():quote_match.end()] + m.group(0)).strip()
            if len(extended) >= min_len:
                return smart_truncate(extended, max_len, min_len)
        # 扩展不到完整句，把金句作为核心观点（较短但完整）
        return full_quote

    # 2. 按句切分，找最长且带冲突/洞见感的句子
    sentences = re.split(r"[。！？\n]+", text)
    keywords = ["差距", "真正", "不是", "偷走", "复利", "算法", "选择", "流向", "被设计成", "默认", "稀缺", "值钱"]
    candidates = []
    for s in sentences:
        s = s.strip().strip("#* >-").strip()
        if not s or len(s) < min_len:
            continue
        score = len(s)
        if any(kw in s for kw in keywords):
            score += 50
        candidates.append((score, s))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        best = candidates[0][1]
        return smart_truncate(best, max_len, min_len)

    # 3. 兜底：正文开头
    return smart_truncate(text, max_len, min_len)


def build_detail_caption(body, topic, hook, voice="清醒陪伴型", insight="反常识", theme=None):
    """基于轮播正文 body，提炼出 3 段约 400 字的小红书详情页文字框。

    要求：
    - 第 1 段：提问 Hook + 场景 + 反转 + 纳瓦尔金句（与正文一致）
    - 第 2 段：论证升级 + 真实纳瓦尔观点背书（不能用无关名人）
    - 第 3 段：阶层/选择反差 + 回扣纳瓦尔观点 + 反问收尾
    - 全文必须与正文中的纳瓦尔思想保持一致，不能独立分割。
    """
    vt = VOICE_TEMPLATES.get(voice, VOICE_TEMPLATES["清醒陪伴型"])
    ia = INSIGHT_ANGLES.get(insight, INSIGHT_ANGLES["反常识"])
    text = (body or "").strip()

    # 去掉系统拼接的标题和核心观点行，避免它们混入详情页提炼
    text = re.sub(r"^#\s+.+\n*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s*💡\s*核心观点：.+\n*", "", text, flags=re.MULTILINE)
    text = text.strip()

    # 按空行拆出自然段（pain / core / section1 / section2 / closing 等）
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    # 按 ## 分页再拆一次，保证小标题后的正文也能被单独使用
    sections = re.split(r"\n##\s+", text)
    bodies = []
    for s in sections:
        s = s.strip()
        if not s:
            continue
        idx = s.find("\n")
        bodies.append(s[idx + 1:].strip() if idx != -1 else s)

    # 从正文中抽取纳瓦尔真实观点，保证详情页与正文一致
    naval_quotes_in_body = re.findall(r"纳瓦尔[说：]*[：:]\s*[『「]([^』」]+)[』」]", text)
    if naval_quotes_in_body:
        naval_thoughts = [f"纳瓦尔说：『{q}』" for q in naval_quotes_in_body[:3]]
    else:
        naval_thoughts = match_naval_thoughts(topic, hook=hook, theme=theme) if theme else []
    if not naval_thoughts and hook:
        naval_thoughts = [f"纳瓦尔说：『{hook}』"]
    if not naval_thoughts:
        naval_thoughts = [
            "纳瓦尔说：『注意力是你最宝贵的资产。』",
            "纳瓦尔说：『如果你拿不定主意，答案就是「不」。』",
        ]
    thought_main = naval_thoughts[0]
    thought_secondary = naval_thoughts[1] if len(naval_thoughts) > 1 else thought_main

    # 第 1 段：从 pain 自然段里提取场景 hook + 反转 + 纳瓦尔金句
    para1_parts = []
    pain_para = paragraphs[0] if paragraphs else (bodies[0] if bodies else text)
    if pain_para:
        # 只在 pain 段落内匹配：从问号到最近句号，不跨段
        qm = re.search(r"[^。！？\n]*\?[^。！？\n]*[。！？]", pain_para.replace("？", "?"))
        if qm:
            para1_parts.append(qm.group(0).strip())
        else:
            # 没有问号的 pain，取完整段但控制在 120 字以内
            para1_parts.append(pain_para[:120] + ("……" if len(pain_para) > 120 else ""))
    # 引号外如果已有标点感，不再重复加句号
    if thought_main.endswith(("』", "」")):
        para1_parts.append(f"{thought_main}但真正让我停下来的，是意识到——{ia}")
    else:
        para1_parts.append(f"{thought_main}。但真正让我停下来的，是意识到——{ia}")
    para1 = " ".join(para1_parts)

    # 第 2 段：从正文第二个自然段/小节压缩 + 真实纳瓦尔观点背书
    para2_parts = []
    second_para = paragraphs[1] if len(paragraphs) > 1 else ""
    if second_para:
        if "纳瓦尔" in second_para:
            # 第二段本身已含纳瓦尔观点，直接用作论证升级，不再重复引用
            para2_parts.append(second_para[:120] + "……")
        else:
            para2_parts.append(second_para[:100] + "……")
            para2_parts.append(f"这正是纳瓦尔的判断：{thought_secondary} 它不是鸡汤，是一个可以立刻检验的行动标准。")
    else:
        para2_parts.append(f"这正是纳瓦尔的判断：{thought_secondary} 它不是鸡汤，是一个可以立刻检验的行动标准。")
    para2 = " ".join(para2_parts)

    # 第 3 段：反差 + 回扣纳瓦尔观点 + 反问
    para3 = f"你选择把{topic}喂给算法，还是选择把它存进自己的复利账户？{thought_main.replace('纳瓦尔说：', '')}答案不在明天，就在你今天做出的下一个选择。"

    caption = f"{para1}\n\n{para2}\n\n{para3}".strip()
    # 控制总长度在 350-450 字之间
    if len(caption) > 450:
        cut = caption[:450]
        last_punct = max(cut.rfind("。"), cut.rfind("？"), cut.rfind("！"))
        if last_punct > 300:
            caption = caption[:last_punct + 1]
    return caption


def short_hook(long_text, max_len=22):
    """把长核心观点截成封面可展示的短 hook。"""
    if not long_text:
        return ""
    if len(long_text) <= max_len:
        return long_text
    cut = long_text[:max_len]
    last_punct = max(cut.rfind("。"), cut.rfind("，"), cut.rfind("；"), cut.rfind("——"))
    if last_punct > 8:
        return long_text[:last_punct + 1]
    return long_text[:max_len] + "…"


def build_import_text(post):
    """把五要素帖子转成『小红书纳瓦尔图文生成器.html』识别的"一键导入"格式。

    该 HTML 的 parseInput 规则：
    - 首行（或 # 标题）→ 封面大字标题
    - | 开头行 → 字数/阅读时间 meta（解析后用于占位，封面副标题会被清空）
    - ## 小标题 → 内文小标题（渲染时自动加 ■）
    - **句子** → 棕色高亮重点句
    - 空行 = 分段；其余按段落自动排版并分页

    映射：
    - cover_title → 首行封面标题
    - word_count / read_time → 第二行 | 全文约 X 字 / 阅读需 Y 分钟 |
    - body（含 ## 分页）→ 内文正文；金句 core_viewpoint 用 ** 包起来高亮
    （注：导入格式有意不附带末尾标签，标签由纳瓦尔生成器自行补，避免重复/错位）
    """
    cover = (post.get("cover_title") or post.get("detail_title") or post.get("title") or "").strip()
    cv = (post.get("core_viewpoint") or "").strip()
    tags = (post.get("tags") or "").strip()
    body = (post.get("body") or "").strip()
    wc = post.get("word_count") or len(re.sub(r"\s", "", body or ""))
    rt = post.get("read_time") or max(1, round(len(body or "") / 400))

    # 1) 正文标准化：丢弃 Markdown 大标题行（首行单独给），清理 ## 小标题前置的 ■（渲染端会自动补）
    norm_lines = []
    for ln in body.split("\n"):
        s = ln.strip()
        if s.startswith("# ") and not s.startswith("## "):
            continue  # 丢弃主标题行
        if s.startswith("## "):
            s = re.sub(r"^##\s*■\s*", "## ", s)
        norm_lines.append(s)
    body_norm = "\n".join(norm_lines).strip()

    # 2) 核心观点（金句）高亮：确保正文里出现一次 **核心观点**
    if cv:
        if ("**" + cv + "**") in body_norm:
            pass  # 已被模型用 ** 包好，不动
        elif cv in body_norm:
            body_norm = body_norm.replace(cv, "**" + cv + "**", 1)
        else:
            # 正文未包含核心观点，则作为开篇金句前置（带高亮）
            body_norm = ("**" + cv + "**\n\n" + body_norm).strip()

    # 3) 组装导入文本
    parts = []
    if cover:
        parts.append(cover)
        parts.append("")
    parts.append(f"| 全文约 {wc} 字 / 阅读需 {rt} 分钟 |")
    parts.append("")
    if body_norm:
        parts.append(body_norm)
    return "\n".join(parts).strip() + "\n"


# ===================== 纳瓦尔核心思想库（真实观点，用于「用纳瓦尔思想解读一切」） =====================
# 本工具的本质是：用纳瓦尔·拉维坎特的真实思想，去解读用户给定的选题方向。
# 纳瓦尔思想是贯穿全文的「透镜」，选题方向只是被解读的「素材」。
NAVAL_CORE_THOUGHTS = {
    "财富": [
        "纳瓦尔说：『财富是你睡觉时仍在为你赚钱的资产，而金钱只是转移这些资产的工具。』他严格区分财富、金钱与地位——追求财富，而非社会地位。",
        "纳瓦尔说：『赚钱是一门可以习得的技能。』它与『假装努力』相反，强调用杠杆放大你独有的专长。",
        "纳瓦尔提出三种杠杆：劳动力、资本、以及零边际成本的杠杆（代码与媒体）。普通人最该掌握的是代码/媒体——不需要任何人的批准。",
    ],
    "判断力": [
        "纳瓦尔说：『判断力是知道长期后果的能力。』在信息过载时代，做正确的判断比拼命努力重要得多。",
        "纳瓦尔：『如果外在表现无法差异化，内在判断必须差异化。』从长远看，阅读比聆听快，做比说快。",
    ],
    "幸福": [
        "纳瓦尔说：『幸福是一种技能，像肌肉一样可以锻炼。』它不是外在条件的奖赏，而是一种可习得的内在状态。",
        "纳瓦尔：『幸福 = 你已拥有的一切 − 你想要的。』欲望是痛苦之源，免于欲望即接近幸福。",
        "纳瓦尔：『平静是成功的代价。』他主张减少外部刺激、向内求得稳定，而非向外索取。",
    ],
    "自由": [
        "纳瓦尔说：『自由是所有目标的目标。』财富、健康、关系的终极目的都是自由。",
        "纳瓦尔主张『退出竞争游戏』：不在别人的规则里内卷，自己定义什么是成功。",
    ],
    "自我产品化/专长": [
        "纳瓦尔说：『找到你天生擅长、且别人觉得像玩耍的事。』专长无法被培训出来，只能由痴迷与好奇长出来。",
        "纳瓦尔：『把自己产品化——先是成为，再是产品化。』先成为不可替代的人，再把能力变成可复制的产品。",
    ],
    "杠杆": [
        "纳瓦尔：『用零边际成本的杠杆——代码、媒体、资本。』你不需要被许可就能使用代码和媒体杠杆。",
        "纳瓦尔：『专长 + 杠杆 = 财富。』没有杠杆的专长，只是辛苦。",
    ],
    "长期主义/复利": [
        "纳瓦尔说：『生活中所有的回报，无论是财富、关系还是知识，都来自复利。』",
        "纳瓦尔主张「长期博弈者」思维：与所有人保持长期可重复的正和博弈。",
    ],
    "说“不”": [
        "纳瓦尔说：『如果你拿不定主意，答案就是「不」。』对无关紧要的事说不，是保护注意力与判断力的武器。",
    ],
    "阅读/学习": [
        "纳瓦尔说：『阅读是你负担得起的最好的杠杆之一。』数学与逻辑是万物基础，大量阅读经典胜过碎片化信息。",
    ],
    "欲望/满足": [
        "纳瓦尔：『幸福是一种无欲无求的状态。』他提醒我们，问题不在得到太少，而在想要太多。",
    ],
    "人际关系": [
        "纳瓦尔说：『你的身份，是你所交往的人的平均值。』环境塑造你，选择圈子就是选择自己。",
        "纳瓦尔主张『远离有毒的人』，把时间与关系投给能托举和滋养你的人。",
    ],
    "注意力": [
        "纳瓦尔说：『注意力是你最宝贵的资产。』在算法时代，谁能守住注意力，谁就守住了自由。",
    ],
}

_NAVAL_THEME_KW = {
    "钱": "财富", "搞钱": "财富", "副业": "财富", "攒钱": "财富", "理财": "财富",
    "穷": "财富", "富": "财富", "焦虑": "幸福", "快乐": "幸福", "满足": "幸福",
    "开心": "幸福", "选择": "判断力", "决策": "判断力", "认知": "判断力",
    "判断": "判断力", "自我": "自我产品化/专长", "成长": "自我产品化/专长",
    "技能": "自我产品化/专长", "专长": "自我产品化/专长", "复利": "长期主义/复利",
    "长期": "长期主义/复利", "人脉": "人际关系", "朋友": "人际关系",
    "圈子": "人际关系", "社交": "人际关系", "人际": "人际关系",
    "刷手机": "注意力", "短视频": "注意力", "算法": "注意力",
    "自由": "自由", "独立": "自由", "说不": "说“不”", "拒绝": "说“不”",
    "读书": "阅读/学习", "阅读": "阅读/学习", "学习": "阅读/学习",
    "想要": "欲望/满足", "欲望": "欲望/满足",
}


def match_naval_thoughts(topic, hook="", theme=None):
    """根据选题方向、钩子与显式纳瓦尔主题，匹配最相关的纳瓦尔真实思想（2-4 条）。"""
    text = f"{topic} {hook} {theme or ''}".strip()
    ordered = []
    seen = set()
    # 1) 显式 theme 优先命中（选题系统传来的纳瓦尔主题）
    if theme:
        for th in str(theme).replace("，", ",").split(","):
            th = th.strip()
            if not th:
                continue
            # 兼容 "财富/金钱" 这种复合键或别名
            core_key = None
            if th in NAVAL_CORE_THOUGHTS:
                core_key = th
            else:
                for k in NAVAL_CORE_THOUGHTS:
                    if th in k or k.startswith(th):
                        core_key = k
                        break
            if core_key:
                for it in NAVAL_CORE_THOUGHTS[core_key]:
                    if it not in seen:
                        ordered.append(it); seen.add(it)
    # 2) 主题名直接命中
    for theme, items in NAVAL_CORE_THOUGHTS.items():
        if theme.split("/")[0] in text or theme in text:
            for it in items:
                if it not in seen:
                    ordered.append(it); seen.add(it)
    # 3) 关键词映射命中
    for kw, theme in _NAVAL_THEME_KW.items():
        if kw in text:
            for it in NAVAL_CORE_THOUGHTS.get(theme, []):
                if it not in seen:
                    ordered.append(it); seen.add(it)
    # 4) 兜底：最普适的纳瓦尔观点
    if not ordered:
        ordered = [
            NAVAL_CORE_THOUGHTS["幸福"][0],
            NAVAL_CORE_THOUGHTS["长期主义/复利"][0],
            NAVAL_CORE_THOUGHTS["自我产品化/专长"][0],
        ]
    return ordered[:4]


# ===================== 真·AI 现写（OpenAI 兼容） =====================

SYSTEM_PROMPT = """你是一位把纳瓦尔·拉维坎特（Naval Ravikant）思想写成小红书爆款的女性向创作者。你的对标标杆是小红书账号「纳瓦尔『启示录』」（同赛道、综合评分81、选题体系9/10），它的内容必须是你的范本：标题情绪化且具体、开篇用具象生活场景代入、纳瓦尔原话只作锚点、通篇温暖第一人称、锁定"内核稳定 / 女性成长 / 探索自我"心智。

你产出的是一篇"完整可发布的小红书帖子"，必须严格遵循以下结构体系（基于对爆款帖子的拆解）：

【五要素结构体系】
1. cover_title（封面/信息流标题）：抓人、女性向、制造反差的钩子标题。长度必须精简，控制在 20 个中文字符以内（含标点）——小红书标题硬性上限，过长的标题会被下游拦截。
2. detail_title（详情页标题）：必须与 cover_title 完全相同——这是"标题一致性锚点"，让用户在信息流、详情页、轮播图任何位置都被同一句话锚定认知。
3. core_viewpoint（核心观点）：从 body 正文中摘出的一段 40-80 字的精彩、吸睛段落（不是口号式短句），要包含反常识洞见或情绪冲突，作为详情页最能拉住人的部分；这段文字必须原样出现在 body 中，并建议加粗。
4. tags（标签）：8-12 个，必须固定包含标杆账号同款心智标签 #纳瓦尔 #内核稳定 #女性成长 #知识库 #探索自我，再补 3-5 个与【选题方向】强相关的具体标签（如人际关系→#人际断舍离 #圈层跃迁；财富→#搞钱思路 #认知觉醒），覆盖身份/方法论/结果/平台热词四层。不得遗漏上述 5 个固定标签。
5. body（图片轮播正文/图文内页）：完整 markdown，按 6 段递进结构撰写——
   认知冲击（痛点共鸣）→ 代入场景 → 认知反转 → 方法论 → 真实切口（用户经历）→ 收尾行动召唤（给出可立即执行的动作）。这是图片里要展示的"完整交付内容"，约 1500-1800 字，用 ## 分页，每页 1-2 个核心信息，小标题前可加 ■，关键句加粗/高亮。
6. detail_caption（详情页文字框）：约 400 字、共 3 段，只负责"勾引"用户点进来看图，不要重复轮播正文，而是给出钩子+前菜+问题缺口。严格遵循：
   - 第 1 段：提问式 Hook + 具体场景（如下班/睡前）+ 反转 + 金句；
   - 第 2 段：论证升级 + 名人/新概念背书（如乔布斯/扎克伯格/盖茨/数字糖果等）+ 给出更高一层框架；
   - 第 3 段：阶层/选择反差 + 反问收尾，制造缺口，让用户必须左滑看图。
   - 风格：无 emoji、无序号、善用破折号"——"、第二人称"你"、对聊感强、严肃知识风。

【选题锚定铁律——最高优先级】
- 全文（封面标题、核心观点、正文、标签）必须紧紧围绕用户给定的【选题方向】展开，绝不允许偷换成其他主题。
- 若【选题方向】是抽象概念（如"财富""幸福""判断力"），必须把它落地为一个具体、可感知的人生命题再展开（例如"财富"→"为什么你越省钱越焦虑"），但落点仍要回扣该概念本身。
- 封面标题与详情页标题必须紧扣【选题方向】，让人一眼看出这篇在讲什么。

【纳瓦尔思想内核铁律——本工具的灵魂（与选题锚定同等最高优先级）】
- 本工具的本质是「用纳瓦尔·拉维坎特的真实思想，去解读用户的选题方向」。纳瓦尔思想是贯穿全文的"透镜"，选题方向只是被解读的"素材"——绝不允许脱离纳瓦尔思想空谈选题。
- 正文必须围绕【纳瓦尔思想透镜】给出的真实观点展开，至少深入展开 1-2 个纳瓦尔核心观点（引用其原话 + 你自己的通俗解读 + 与选题的结合），让通篇透出鲜明的"纳瓦尔味"。
- 纳瓦尔思想不是装饰，而是论证的主线：痛点→反转→方法论，每一步都要落到纳瓦尔的一个具体洞见上。
- 禁止把纳瓦尔简化成"鸡汤符号"；必须呈现他真实、有时冷峻的观点（如"欲望是痛苦之源""自由是所有目标的目标""你交往的人是你自己的平均值""幸福是一种技能"），并用大白话翻译给女性读者。

【写作风格铁律——对标「纳瓦尔启示录」】
- 标题范例（情绪 / 具体 / 有钩子，≤20字）："千万不要恐惧，事情会以奇怪的方式解决" ／ "要去到能托举和滋养你的地方" ／ "让人更尊重你的微妙行为" ／ "如果你不知道怎么照顾自己，可以从这个清单"。
- 禁止生硬套版标题，例如"真正拉开女生差距的，不是努力，是X""你不是懒，你只是被设计了X"这类空洞对仗；严禁标题党。
- 开篇必须用【具体生活场景】代入（如等待一条消息的焦虑、工作日夜晚的报复性熬夜），不要一上来就讲大道理。
- 纳瓦尔原话（用户提供的金句）只作为【锚点金句】嵌入一处，不堆砌；其余论证用你自己的话、具体、有人味。
- 真实大于完美：必须融入用户"真实切口"，第一人称、具体场景、可感知情绪，像闺蜜聊天而非说教。
- 独到见解：不重复烂大街鸡汤，给读者"啊原来还能这样想"的反转，结合纳瓦尔又超越纳瓦尔。
- 工具/清单型定位（收藏>点赞规律）：给读者能立刻执行的清单或动作，而非纯情绪共鸣。
- 女性视角：平视、陪伴、共情，把抽象道理翻译成"她也能做到"。
- 禁止空泛反常识套话、禁止不落地地堆砌"复利/杠杆/多巴胺/算法"等概念词；每个概念必须配一个具体的人或事。

你必须只返回一个 JSON 对象（不要任何额外文字、不要 markdown 代码块标记），结构如下：
{
  "cover_title": "封面/信息流钩子标题（抓人、女性向、不标题党）",
  "detail_title": "详情页标题（与 cover_title 完全相同）",
  "core_viewpoint": "从 body 正文中摘出的 40-80 字精彩段落（含反常识洞见/情绪冲突），必须原样出现在 body 中，作为详情页最吸睛部分",
  "tags": ["#身份标签","#方法论标签","#结果标签","#平台热词", "..."],
  "body": "图片轮播正文：用 ## 分页（痛点共鸣 / 认知反转 / 方法论 / 真实切口 / 数据打脸 / 收尾行动召唤），每段要有可截图金句。约 1500-1800 字。注意：① 封面标题和核心观点由系统统一拼接，body 内不要再写主标题和核心观点行；② tags 是独立字段，body 末尾以及每一页内页都不要附加 tags 或提示词。",
  "detail_caption": "详情页文字框：3 段约 400 字，必须与正文联动、不能独立分割。要求：① 第 1 段用提问 Hook+场景+反转，并嵌入一条正文中出现的纳瓦尔观点；② 第 2 段论证升级，引用同一条或另一条正文中的纳瓦尔真实观点做背书（禁止用与正文无关的名人/新概念）；③ 第 3 段阶层/选择反差+回扣纳瓦尔观点+反问收尾。全文必须让人感觉'详情页就是正文精华的钩子版'，而不是另一篇独立文案。"
}"""


def build_user_prompt(topic, hook, voice, insight, cut, real_notes, brief=None, strategy=None, theme=None):
    vt = VOICE_TEMPLATES.get(voice, VOICE_TEMPLATES["清醒陪伴型"])
    ia = INSIGHT_ANGLES.get(insight, INSIGHT_ANGLES["反常识"])
    notes_text = "\n".join("- " + t for t in (real_notes or [])[:15]) or "（无）"
    cut_text = cut if cut else "（用户未提供，请用温和的普适性第一人称示例，并提示她可替换为自己的故事）"

    brief_text = ""
    if brief:
        brief_text = "\n【调研简报（来自红狐真实爆款 + AI 深度分析，必须参考）】\n" + json.dumps(brief, ensure_ascii=False, indent=2)
    strat_text = ""
    strat_map = ""
    if strategy:
        strat_text = "\n【差异化内容策略（必须严格遵循）】\n" + json.dumps(strategy, ensure_ascii=False, indent=2)
        da = strategy.get("differentiated_angle")
        to = strategy.get("title_options")
        bp = strategy.get("structure_blueprint")
        mic = strategy.get("must_include_cases")
        av = strategy.get("avoid")
        if da:
            strat_map += f"\n- core_viewpoint（核心观点）必须体现这个差异化角度：{da}"
        if to:
            strat_map += f"\n- cover_title / detail_title 优先从以下备选里选或化用：{' ／ '.join(to)}"
        if bp:
            strat_map += f"\n- body 六段结构蓝图（务必遵循）：{' ｜ '.join(bp)}"
        if mic:
            strat_map += f"\n- body 中必须嵌入的真实案例：{'；'.join(mic)}"
        if av:
            strat_map += f"\n- body 中必须避开的说法：{'；'.join(av)}"

    naval_theme_text = f"（本选题锁定的纳瓦尔主题：{theme}）" if theme else ""
    naval_lens = match_naval_thoughts(topic, hook, theme=theme)
    naval_lens_text = "\n".join(f"- {t}" for t in naval_lens)

    return f"""请根据以下要素，生成一篇完整可发布的小红书帖子（严格遵循"五要素结构体系"）。

【选题方向（全文最高优先级·标题/正文/标签必须紧扣此方向，不得偷换主题；若方向偏抽象请落地为具体人生命题）】{topic}
【本选题锁定的纳瓦尔主题（生成时必须紧扣这些主题，不得偏离）】{theme or '由模型根据选题自行把握'}
【纳瓦尔原典金句/钩子】{hook}
【纳瓦尔思想透镜（必须用这些真实思想来解读选题方向；正文至少深入展开其中 1-2 条，让通篇透出纳瓦尔味）】{naval_theme_text}
{naval_lens_text}
【目标语气】{voice}（{vt['开场称呼']}开场，句式：{vt['句式特点']}，结尾风格：{vt['结尾']}）
【独到见解角度】{insight}（角度说明：{ia}）
【真实切口（必须融入 body 真实切口段，用第一人称）】{cut_text}
【真实爆款参考标题（用于把握当下流量角度，不要抄袭，要超越）】
{notes_text}
{brief_text}
{strat_text}
{strat_map}

要求（五要素必须齐备，且联动调研与策略）：
- cover_title 与 detail_title 必须是同一句话（标题一致性锚点）。
- core_viewpoint 是 40-80 字的精彩长段落（从 body 中摘出，不是口号），含反常识洞见或情绪冲突，必须原样出现在 body 中（建议加粗），作为详情页最吸睛部分。
- tags 至少 8 个，覆盖 身份/方法论/结果/平台热词 四层。
- body（图片轮播正文）6 段递进：痛点共鸣 → 认知反转 → 方法论 → 真实切口（含用户经历）→ 数据打脸 → 收尾行动召唤（给可立即执行的动作），约 1500-1800 字，用 ## 分页，每页有 1-2 个核心信息。
- detail_caption（详情页文字框）约 400 字 3 段，必须与 body 中的纳瓦尔思想一致、不能独立分割：第 1 段提问 Hook+场景+反转+纳瓦尔金句；第 2 段论证升级+正文中的纳瓦尔真实观点背书（禁止用无关名人/新概念）；第 3 段阶层反差+回扣纳瓦尔观点+反问收尾。让人感觉详情页就是正文精华的钩子版。
- 必须参考【调研简报】中的真实案例/数据增强深度；遵循【差异化内容策略】的结构蓝图与反常识角度；避开策略里列出的烂大街说法。
- 【纳瓦尔味强制要求】纳瓦尔思想必须贯穿全文，正文至少深入展开 1-2 个【纳瓦尔思想透镜】中的核心观点（引用原话+你自己的通俗解读+与选题的结合），让人读完整篇能明确感受到"这是讲纳瓦尔思想的小红书笔记"，而非脱离纳瓦尔空谈选题。
- 独到见解必须具体到"这个观点新在哪"；女性向、人味、可复用。
- 严格按 SYSTEM 要求的 JSON 输出（cover_title/detail_title/core_viewpoint/tags/body/detail_caption），只返回 JSON，不要包裹代码块。"""


def call_openai_compatible(system_prompt, user_prompt, api_key, base_url, model, timeout=150):
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.9,
        "stream": False,
    }
    last_err = None
    # 上游模型偶有 5xx/连接抖动，自动重试 2 次，避免用户端偶发 500
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if r.status_code != 200:
                # 5xx 可重试；4xx（401/429 等）直接抛错
                if 500 <= r.status_code < 600 and attempt < 2:
                    last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                    time.sleep(2 + attempt * 2)
                    continue
                raise Exception(f"HTTP {r.status_code}: {r.text[:400]}")
            data = r.json()
            return data["choices"][0]["message"]["content"]
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            if attempt < 2:
                time.sleep(2 + attempt * 2)
                continue
            raise Exception(f"调用 AI 失败：{e}")
    raise Exception(f"调用 AI 失败（重试后仍失败）：{last_err}")


def package_ai_output(data, topic, brief=None, strategy=None, hook="", voice="清醒陪伴型", insight="反常识", theme=None):
    """把 AI 返回的 JSON 数据统一打包成前后端一致的帖子结构（AI 模式与改写模式复用）。"""
    cover = (data.get("cover_title") or data.get("title") or topic).strip()
    detail = (data.get("detail_title") or cover).strip()
    if detail != cover:
        detail = cover  # 标题一致性锚点：强制同步
    cv = (data.get("core_viewpoint") or "").strip()
    raw_tags = data.get("tags") or []
    # 统一解析为小红书可识别格式（#话题1 #话题2，空格分隔）
    tags_str, tags_list = normalize_xhs_tags(raw_tags)
    # 强制补齐标杆账号「纳瓦尔启示录」同款固定心智标签，保证内容心智一致（不依赖模型自觉）
    FIXED_TAGS = ["#纳瓦尔", "#内核稳定", "#女性成长", "#知识库", "#探索自我"]
    for ft in FIXED_TAGS:
        if ft not in tags_list:
            tags_list.append(ft)
    tags_str = " ".join(tags_list) if tags_list else ""
    body = (data.get("body") or data.get("markdown") or "").strip()
    # 防御：即使模型在正文末尾附了标签，也剥离，确保图文内页不出现标签
    body = strip_trailing_tags(body)

    # 核心观点必须是正文里的精彩长段落（40-80 字）；如果模型返回太短或不在正文中，兜底提取
    if not cv or len(cv) < 30 or cv not in body:
        cv = extract_core_viewpoint(body, fallback=cv)
    # 确保核心观点确实出现在 body 中（SYSTEM 的铁律）
    if cv and cv not in body:
        body = f"**{cv}**\n\n{body}"
    cover_subtitle = short_hook(cv)

    # 详情页文字框：若模型没给，从 body 自动提炼 3 段约 400 字，必须和纳瓦尔主题一致
    detail_caption = (data.get("detail_caption") or "").strip()
    if not detail_caption:
        detail_caption = build_detail_caption(body, topic, hook, voice, insight, theme=theme)
    detail_caption = clean_content_symbols(detail_caption)
    body = clean_content_symbols(body)

    # 组装完整 markdown（封面标题 + 核心观点 + 正文 + 标签）
    parts = [f"# {cover}", ""]
    if cv:
        parts.append(f"> 💡 核心观点：{cv}")
        parts.append("")
    if body:
        parts.append(body)
    if tags_str and not any(t in (body or "") for t in tags_list):
        parts.append("")
        parts.append(tags_str)
    markdown = "\n".join(parts).strip()

    # 拆页预览：封面 + 各 ## 段
    sections = re.split(r"\n##\s+", body)
    pages = [{"type": "cover", "title": cover, "subtitle": cover_subtitle}]
    for s in sections:
        s = s.strip()
        if not s:
            continue
        lines = s.split("\n", 1)
        ptitle = lines[0].strip()
        pbody = lines[1].strip() if len(lines) > 1 else ""
        pages.append({"type": "page", "title": ptitle, "body": pbody})

    wc = len(markdown.replace(" ", "").replace("\n", ""))
    rt = max(1, round(len(markdown) / 400))
    return {
        "title": cover,
        "cover_title": cover,
        "detail_title": detail,
        "core_viewpoint": cv,
        "tags": tags_str,
        "tags_list": tags_list,
        "body": body,
        "detail_caption": detail_caption,
        "subtitle": cover_subtitle,
        "word_count": wc,
        "read_time": rt,
        "pages": pages,
        "markdown": markdown,
        "mode": "ai",
        "brief": brief,
        "strategy": strategy,
    }


def generate_with_ai(topic, hook, voice, insight, cut, real_notes, api_key, base_url, model, brief=None, strategy=None, theme=None):
    up = build_user_prompt(topic, hook, voice, insight, cut, real_notes, brief, strategy, theme=theme)
    raw = call_openai_compatible(SYSTEM_PROMPT, up, api_key, base_url, model)
    raw = strip_code(raw)
    try:
        data = json.loads(raw)
        return package_ai_output(data, topic, brief=brief, strategy=strategy, hook=hook, voice=voice, insight=insight, theme=theme)
    except Exception as e:
        return {
            "title": topic,
            "cover_title": topic,
            "detail_title": topic,
            "core_viewpoint": "",
            "tags": "",
            "tags_list": [],
            "body": raw,
            "subtitle": "AI 返回解析失败，已原样展示",
            "word_count": len(raw),
            "read_time": max(1, round(len(raw) / 400)),
            "pages": [{"type": "page", "title": "AI 原始输出", "body": raw}],
            "markdown": raw,
            "mode": "ai",
            "warn": f"AI 返回非标准 JSON，已原样展示：{e}",
        }


# ===================== 首条评论AI：引发深度讨论 =====================

FIRST_COMMENT_SYSTEM = """你是一位资深的小红书内容运营，擅长为刚发布的笔记写一条"置顶首评"。
这条首评的核心目的不是复述文章，而是【激活评论区、引发读者深度思考与讨论】。

写作原则：
1. 必须紧扣文章核心议题，绝不跑题。
2. 给出有启发性的观点 / 合理的质疑 / 延伸的视角，引导读者突破表层信息，往更深处反思。
3. 语气自然贴合文章风格（见下方"文章语气/风格"），像一位认真读完后留下思考的真读者，而不是官方总结或机器人。
4. 绝对避免简单复述文章原文；要点出文章没明说、但读者本该想到的事。
5. 克制、真诚、有具体抓手（一个反问 / 一个反例 / 一个真实困境），让人想接话。
6. 长度：30-90 字，一两条微信体段落即可；不要分点列表、不要 emoji 刷屏、不要"楼主说得对"这类无效捧场。
7. 严格只返回一个 JSON 对象（不要 markdown 代码块标记），结构如下：
{
  "comment": "首评正文（紧扣议题、能引发深度讨论，不复述原文）",
  "discussion_hook": "这条评为什么能引发讨论（仅内部说明，1 句，不展示给用户）"
}

策略偏好（由调用方指定，可多为空，为空则自行判断）：
- 观点延伸：在文章结论上往前再推一步，打开更大画面。
- 合理质疑：对文章里的隐含前提提出温和但有力的一问。
- 延伸视角：换一个文章没覆盖的角度（如代价、长期、旁观者、反方）。
- 开放反问：用一个没有标准答案的真问题收尾，把话筒交还给读者。"""


def build_local_first_comment(title, body, topic, voice, strategy=""):
    """无大模型 Key 时的本地模板首评：围绕议题抛出一个能引发讨论的真问题。"""
    seed = (title or topic or "这个话题").strip().strip("#")
    strategy = (strategy or "").strip()
    if strategy == "合理质疑":
        return (f"看完有个忍不住想杠的点：文章把「{seed}」讲成了几乎纯收益的事，"
                f"但很少有人算过它的隐性代价——时间、情绪、机会成本。你真的确认这笔账划算吗？")
    if strategy == "延伸视角":
        return (f"补一个文章没覆盖的角度：当我们都在讨论「{seed}」怎么「做对」时，"
                f"有没有人想过，有些人连「想这件事」的带宽都被日常吞掉了？视角一换，问题就变了。")
    if strategy == "开放反问":
        return (f"文章给了答案，但我觉得更值得问的是：如果「{seed}」真的是对的，"
                f"为什么我们身边做到的人这么少？是做不到，还是根本不想？欢迎来辩。")
    # 默认：观点延伸
    return (f"顺着文章再往前推一步：「{seed}」真正难的不是知道，而是日复一日地不背叛自己的判断。"
            f"道理大家都懂，能落进每一天的人为什么这么少？这届评论区里，有人做到过吗？")


def generate_first_comment(title, body, topic, voice, strategy, api_key, base_url, model, timeout=45):
    """调用大模型，为文章生成一条能引发深度讨论的置顶首评。返回 (comment, discussion_hook)。"""
    body_excerpt = (body or "")[:1200]
    strat_text = f"\n【本次指定的策略偏好】{strategy}" if strategy else ""
    user = f"""【文章标题】{title or '（未提供）'}
【文章核心议题/选题】{topic or title or '（未提供）'}
【文章语气/风格】{voice or '（未提供，请判断并贴合）'}
【文章正文（用于把握议题与风格，可摘取，不要复述）】
{body_excerpt or '（未提供正文，仅依据标题生成）'}
{strat_text}

请生成一条置顶首评，要求：紧扣议题、提出启发性观点/合理质疑/延伸视角、引导读者突破表层进行深度反思与讨论、语气贴合文章风格、绝不复述原文。只返回 JSON。"""
    raw = call_openai_compatible(FIRST_COMMENT_SYSTEM, user, api_key, base_url, model, timeout=timeout)
    raw = strip_code(raw)
    try:
        data = json.loads(raw)
        comment = (data.get("comment") or "").strip()
        hook = (data.get("discussion_hook") or "").strip()
        if not comment:
            raise ValueError("comment 为空")
        return comment, hook
    except Exception as e:
        raise RuntimeError(f"首评解析失败：{e} | raw: {raw[:200]}")


REWRITE_SYSTEM = """你是一位小红书爆款改写编辑。你的任务是：根据【QC 质检报告】的反馈，对【当前帖子】进行精准改写，输出一版质量更高、去 AI 味、结构更紧、人味更足的新帖子。

改写原则：
1. 严格遵循"五要素结构体系"：cover_title / detail_title / core_viewpoint / tags / body / detail_caption。
2. 必须逐条回应 QC 报告里的 issues 和 suggestions，不要忽略任何一条。
3. 保留原有选题方向、语气、独到见解角度、真实切口（用户经历），但表达要更自然、更像真人笔记。
4. core_viewpoint 必须是 body 中摘出的 40-80 字精彩长段落（不是口号），含反常识洞见或情绪冲突。
5. body 是图片轮播正文，用 ## 分页撰写，约 1500-1800 字，不要重复写主标题和核心观点行；body 末尾以及每一页内页都不要附加 tags。
6. detail_caption 是详情页文字框，约 400 字 3 段，只负责勾引：第 1 段提问 Hook+场景+反转+金句；第 2 段论证升级+名人/新概念背书；第 3 段阶层反差+反问收尾。不要重复 body。
7. 输出必须只返回一个 JSON 对象，不要 markdown 代码块，不要多余说明。

JSON 结构：
{
  "cover_title": "封面标题（与 detail_title 完全一致）",
  "detail_title": "详情页标题（与 cover_title 完全一致）",
  "core_viewpoint": "从 body 中摘出的 40-80 字精彩段落",
  "tags": ["#身份标签", "#方法论标签", "#结果标签", "#平台热词", "..."],
  "body": "图片轮播正文 markdown，用 ## 分页，约 1500-1800 字",
  "detail_caption": "详情页文字框，3 段约 400 字，只负责勾引"
}"""


def build_rewrite_prompt(topic, hook, voice, insight, cut, markdown, qc_report):
    issues = qc_report.get("issues", [])
    suggestions = qc_report.get("suggestions", [])
    ai_smell = qc_report.get("ai_smell", [])
    sections = []
    sections.append(f"【选题方向（全文必须紧扣此方向，不得偷换主题）】{topic}")
    sections.append(f"【纳瓦尔钩子】{hook}")
    sections.append(f"【目标语气】{voice}")
    sections.append(f"【独到见解角度】{insight}")
    sections.append(f"【真实切口】{cut or '（用户未提供）'}")
    sections.append(f"【当前质量分】{qc_report.get('score', '—')} / 100")
    sections.append(f"【AI 味问题】{ai_smell if ai_smell else '无明显问题'}")
    sections.append(f"【具体问题】{issues if issues else '无'}")
    sections.append(f"【改写建议】{suggestions if suggestions else '无'}")
    sections.append(f"\n【当前帖子全文】\n{markdown}\n")
    return "\n".join(sections)


def rewrite_with_ai(topic, hook, voice, insight, cut, markdown, qc_report, api_key, base_url, model, theme=None):
    up = build_rewrite_prompt(topic, hook, voice, insight, cut, markdown, qc_report)
    if theme:
        up = f"【本选题锁定的纳瓦尔主题（改写后必须仍然紧扣）】{theme}\n\n" + up
    raw = call_openai_compatible(REWRITE_SYSTEM, up, api_key, base_url, model)
    raw = strip_code(raw)
    data = json.loads(raw)
    return package_ai_output(data, topic, hook=hook, voice=voice, insight=insight, theme=theme)


# ===================== 调研AI：捕捉最新 + 找观点空白 =====================

RESEARCH_SYSTEM = """你是一位资深的小红书爆款研究员，擅长从真实爆款数据中提炼可复用的内容规律。
你的任务：基于用户给的选题和一批真实爆款标题，输出一份结构化调研简报，用于指导后续内容生产。
你必须只返回一个 JSON 对象（不要任何额外文字、不要 markdown 代码块标记），结构如下：
{
  "hot_titles": ["真实爆款标题1", "标题2"],
  "pain_points": ["高频痛点1", "痛点2"],
  "angle_gaps": ["已有爆款都在说X，但没人说Y", "观点空白2"],
  "latest_cases": ["可用于正文的真实案例/数据1", "案例2"],
  "recommended_voice": "清醒陪伴型（并说明理由）",
  "recommended_insight": "反常识（并说明理由）",
  "core_quote": "一句可作为文章核心钩子的纳瓦尔风格金句"
}"""


def research_topic(topic, hot_titles, api_key, base_url, model, extra_insights=None):
    titles_text = "\n".join("- " + t for t in (hot_titles or [])[:25]) or "（无）"
    extra = extra_insights or {}
    extras = []
    # 封面/趋势/周榜：结构为分类键（low_fan_explosive 等）或 markdown，递归抽取标题，缺则回退 markdown
    for key, label in [
        ("covers", "同赛道爆款封面标题"),
        ("trends", "近期爆款趋势标题"),
        ("weekly", "近7日垂直领域热榜标题"),
    ]:
        src = extra.get(key)
        if not isinstance(src, dict):
            continue
        titles = []
        _collect_titles_recursive(src, titles, 25)
        if titles:
            extras.append(f"【{label}】\n" + "\n".join("- " + t for t in titles[:8]))
        else:
            md = (src.get("markdown") or src.get("raw") or "").strip()
            if md:
                extras.append(f"【{label}】\n" + md[:800])
    if extra.get("top_accounts"):
        accounts = extra["top_accounts"]
        if isinstance(accounts, list):
            extras.append("【同领域热门账号】\n" + "\n".join("- " + str(a) for a in accounts[:6]))
        elif isinstance(accounts, dict):
            md = (accounts.get("markdown") or accounts.get("raw") or "").strip()
            if md:
                extras.append("【同领域热门账号榜】\n" + md[:800])
    if extra.get("similar_accounts"):
        sims = extra["similar_accounts"]
        if isinstance(sims, list):
            extras.append("【可直接复制的同阶对标账号】\n" + "\n".join("- " + str(s) for s in sims[:6]))
        elif isinstance(sims, dict):
            md = (sims.get("markdown") or sims.get("raw") or "").strip()
            if md:
                extras.append("【可直接复制的同阶对标账号】\n" + md[:800])
    extras_text = "\n\n".join(extras) or "（无其他维度的补充数据）"

    user = f"""【选题】{topic}
【真实爆款标题（来自红狐接口，反映当下真实流量）】
{titles_text}

{extras_text}

请基于以上多维真实数据做深度调研，输出 JSON 调研简报。要求：
- pain_points 必须来自真实标题折射的用户焦虑，不要空泛。
- angle_gaps 必须具体指出"别人没说的空白"，这是差异化关键。
- latest_cases 给出可嵌入正文的真实案例或数据（如科技巨头育儿、复利、注意力账单、热门账号做法等），增强深度。
- recommended_voice / recommended_insight 给出明确推荐及理由。
- 严格只返回 JSON。"""
    raw = call_openai_compatible(RESEARCH_SYSTEM, user, api_key, base_url, model)
    raw = strip_code(raw)
    try:
        return json.loads(raw)
    except Exception:
        return {"error": "调研简报解析失败", "raw": raw[:800]}


# ===================== 蒸馏AI：把调研变成差异化策略 =====================

DISTILL_SYSTEM = """你是内容策略总监，负责把调研简报转化成一份"差异化内容策略"，指导写手写出超越爆款的文章。
你必须只返回一个 JSON 对象（不要额外文字、不要 markdown 代码块），结构如下：
{
  "differentiated_angle": "这篇具体新在哪、反转让读者'啊哈'的点",
  "structure_blueprint": ["第1页封面金句", "第2页痛点共鸣（具体论点）", "第3页认知反转（具体论点）", "第4页方法论（具体论点）", "第5页真实切口（具体论点）", "第6页收尾互动（具体论点）"],
  "must_include_cases": ["必须嵌入的真实案例1", "案例2"],
  "avoid": ["要避免的烂大街说法1", "说法2"],
  "title_options": ["标题1", "标题2", "标题3"]
}"""


def distill_brief(brief, api_key, base_url, model):
    brief_text = json.dumps(brief, ensure_ascii=False, indent=2)
    user = f"【调研简报】\n{brief_text}\n\n请输出差异化内容策略 JSON。要求：differentiated_angle 必须具体到'这个观点新在哪'；structure_blueprint 六页每页给出明确论点；avoid 列出常见 AI/鸡汤套话。严格只返回 JSON。"
    raw = call_openai_compatible(DISTILL_SYSTEM, user, api_key, base_url, model)
    raw = strip_code(raw)
    try:
        return json.loads(raw)
    except Exception:
        return {"error": "策略解析失败", "raw": raw[:800]}


# ===================== 选题工坊：系统化关键词矩阵 =====================

# ===================== 选题工坊：红狐真实爆款 + AI 反向组合 =====================

IDEATE_SYSTEM = """你是一位资深小红书选题策划，擅长从真实爆款数据反推"值得写、且能超越现有爆款"的选题。

你会拿到两类素材：
1) 一批【真实爆款标题】（来自红狐数据接口，反映当下平台真实的流量热点、用户痛点与表达腔调）。
2) 纳瓦尔·拉维坎特（Naval Ravikant）的核心思想主题：财富、判断力、幸福、杠杆、专长、复利、自由、学习、决策、注意力、关系、健康、时间、产品化。

你的任务：结合两者，反向组合出【女性向、有独到见解、能超越现有爆款】的选题包。
核心方法——"升维借壳"：
- 不要照搬爆款标题，而是识别它背后的【情绪/场景/痛点】，用纳瓦尔的某个概念做"更高一层的解释框架"。
- 例：爆款"穷人上瘾的东西富人碰都不碰" → 借壳成"女生最容易被偷走的复利账户，是注意力" + 纳瓦尔"注意力是最宝贵资产"。
- 优先产出女性更容易代入的场景：职场、关系、消费、自我成长、年龄焦虑、时间贫困。

每条选题必须包含：
- topic：具体选题关键词（女性成长场景 + 纳瓦尔概念，8-18字）
- naval_quote：一句纳瓦尔原典金句（中文翻译，准确、有金句感）
- cut_template：一个真实切口模板（第一人称或身边人故事，有具体细节与情绪，可让读者代入，30-60字）
- recommended_voice：从[清醒陪伴型/反骨警示型/温柔坚定型/算账拆解型/故事共鸣型/观点刺穿型]选最合适的一种
- recommended_insight：从[反常识/女性专属/算账视角/亲身经历/二元对立/身份重构]选最合适的一种
- tags：8-10个分层标签，必须覆盖"身份层(#女性成长 #女生必看 等)+方法层(#纳瓦尔 #认知升级 等)+结果层(#人间清醒 #自我提升 等)+平台热词层(#拒绝内耗 #女性力量 等)"

你必须只返回一个 JSON 对象（不要额外文字、不要 markdown 代码块），结构如下：
{
  "ideas": [
    {"topic":"...","naval_quote":"...","cut_template":"...","recommended_voice":"...","recommended_insight":"...","tags":"#... #..."},
    ...
  ]
}"""


def ideate_with_redfox(seed, hot_titles, api_key, base_url, model, count=6, existing_items=None):
    """结合红狐真实爆款标题 + 纳瓦尔主题，调用 AI 反向组合选题包。

    传入 existing_items 时，要求 AI 生成与已有选题不重复的新选题。
    单次请求最大生成 30 条（防止 AI 输出过长/解析失败），
    如果 count 超过 30，会拆成多次请求。
    """
    existing_items = existing_items or []
    existing_topics = [it.get("topic", "") for it in existing_items if it.get("topic")]
    existing_block = "\n".join(f"- {t}" for t in existing_topics[:30]) if existing_topics else "（无已有选题）"
    titles_block = "\n".join(f"- {t}" for t in hot_titles[:25]) if hot_titles else "（无真实爆款数据）"

    per_batch = min(count, 30)
    all_ideas = []
    batches = max(1, (count + per_batch - 1) // per_batch)

    for batch in range(batches):
        need = min(per_batch, count - len(all_ideas))
        if need <= 0:
            break
        user = f"""【红狐真实爆款标题（当下平台流量热点）】
{titles_block}

【种子词】{seed or '（无，请自由发散）'}
【需要生成数量】{need}
【已生成过的选题（禁止重复）】
{existing_block}
{"\n".join(f"- {it.get('topic', '')}" for it in all_ideas[:20]) or '（本批次已生成：无）'}

请结合真实爆款的"情绪/场景/痛点"与纳瓦尔思想，反向组合出 {need} 条女性向选题包。
要求：
1. 严格与【已生成过的选题】不重复；
2. 每条选题要有独特角度，避免同质化；
3. 只返回 JSON，不要额外文字。"""
        raw = call_openai_compatible(IDEATE_SYSTEM, user, api_key, base_url, model, timeout=45)
        raw = strip_code(raw)
        try:
            data = json.loads(raw)
            ideas = data.get("ideas", [])
            if not ideas:
                raise ValueError("AI 返回 ideas 为空")
            for it in ideas:
                topic = it.get("topic", "").strip()
                if not topic:
                    continue
                scene = it.get("scene", topic).strip()
                naval_themes = _infer_naval_themes(topic, scene)
                real_theme = naval_themes[0]
                all_ideas.append({
                    "theme": real_theme,  # 用真实纳瓦尔主题，确保选题库筛选与后续生成连贯
                    "scene": scene,
                    "topic": topic,
                    "naval_quote": it.get("naval_quote", "把注意力收回来，是你能给你最好的投资。"),
                    "cut_template": it.get("cut_template", ""),
                    "recommended_voice": it.get("recommended_voice", "清醒陪伴型"),
                    "recommended_insight": it.get("recommended_insight", "反常识"),
                    "tags": it.get("tags", ""),
                    "source": "redfox+ai",
                })
        except Exception as e:
            # 如果某一批失败，保留已生成的
            if not all_ideas:
                raise RuntimeError(f"AI 反向组合解析失败：{e} | raw: {raw[:300]}")
            break

    return all_ideas


def _related_keywords(seed):
    """根据种子词返回若干相关红狐搜索关键词，用于批量调取更多真实爆款（也更多消耗积分）。"""
    seed = (seed or "").strip()
    matched = [th for th in TOPIC_MATRIX if seed and (seed in th or th in seed)]
    if matched:
        return matched[:3] + ["女性成长"]
    # 无匹配则取前 2 个主题 + 女性成长
    return list(TOPIC_MATRIX.keys())[:2] + ["女性成长"]


def _finalize_candidates(raw_ideas, existing_items, hot_title_strs, api_key="", base_url="", model="Auto"):
    """对原始候选做：富化评分 → 关键词+jaccard 语义去重 → 可选 AI 判重 → 综合分排序。"""
    used_items = [i for i in existing_items if i.get("status") in ("used", "archived")]
    # 1) 富化：补评分/综合分/理由/状态
    enriched = [_enrich_idea(it, hot_title_strs=hot_title_strs, used_items=used_items) for it in raw_ideas]
    # 2) 关键词 + jaccard 语义去重（与全库比对，避免重复入库）
    kept = []
    for it in enriched:
        dup = False
        for old in existing_items + kept:
            if is_similar_idea(it, old) or _jaccard(it.get("topic", ""), old.get("topic", "")) >= 0.5:
                dup = True
                break
        if not dup:
            kept.append(it)
    # 3) AI 语义判重（第二档，仅对已发布项；失败不影响，退回关键词去重）
    used_topics = [i.get("topic", "") for i in used_items]
    ai_dup = ai_semantic_dedupe(kept, used_topics, api_key, base_url, model)
    if ai_dup:
        kept = [it for idx, it in enumerate(kept) if idx not in ai_dup]
    # 4) 按综合分降序
    kept.sort(key=lambda x: x.get("composite", 0), reverse=True)
    return kept


def generate_ideas(seed="", count=6, use_redfox=False, api_key="", base_url="", model="Auto", redfox_api_key=None):
    """生成系统化选题包（v2：生成即打分 + 语义去重 + 综合分排序 + 入库为草稿）。

    模式一（use_redfox=False）：纯本地矩阵（TOPIC_MATRIX × 模板化金句库 × CUT_TEMPLATES）。
    模式二（use_redfox=True）：强制先调红狐拉真实爆款（真实消耗积分），
      再让 AI 结合纳瓦尔主题反向组合；若未配置大模型 Key，则用本地模板批量组合。
    所有候选都会：① 用「热度×纳瓦尔契合×场景 − 重复惩罚」打分并生成推荐理由；
    ② 与选题库语义去重；③ 按综合分排序后入库为 draft，支持智能推荐复用。
    """
    seed = (seed or "").strip()
    try:
        count = max(1, int(count) if str(count).isdigit() else 6)
    except Exception:
        count = 6

    bank = load_idea_bank()
    existing_items = bank.get("items", [])
    themes = list(TOPIC_MATRIX.keys())

    raw_ideas = None
    source = "local"
    hot_title_strs = []
    warn = None

    if use_redfox:
        hot_titles = []
        try:
            search_keyword = seed or "女性成长"
            search_keywords = [search_keyword]
            if count >= 30:
                search_keywords += _related_keywords(seed)[:2]  # 最多再扩 2 个相关词
            _sk_seen = set()
            search_keywords = [k for k in search_keywords if not (k in _sk_seen or _sk_seen.add(k))]
            for kw in search_keywords:
                try:
                    sr = run_search(kw, max_items=30, redfox_api_key=redfox_api_key)
                    if isinstance(sr, dict) and "items" in sr:
                        for it in sr["items"]:
                            if isinstance(it, dict) and it.get("title"):
                                hot_titles.append(it)
                except Exception:
                    pass
            # 按标题去重，避免重复爆款算多条
            _seen_t = set(); _uniq = []
            for it in hot_titles:
                t = it.get("title", "")
                if t and t not in _seen_t:
                    _seen_t.add(t); _uniq.append(it)
            hot_titles = _uniq
            hot_title_strs = [it.get("title", "") for it in hot_titles]
        except Exception:
            hot_titles = []; hot_title_strs = []

        if hot_titles:
            source = "redfox+ai"
            try:
                if api_key:
                    raw_ideas = ideate_with_redfox(seed, hot_title_strs, api_key, base_url, model, count, existing_items)
                else:
                    raw_ideas = build_ideas_from_hot_titles(seed, hot_titles, count, existing_items)
                    warn = "未配置大模型 Key，选题由红狐真实爆款 + 本地模板组合生成（仍消耗红狐积分）。"
            except Exception as e:
                raw_ideas = build_ideas_from_hot_titles(seed, hot_titles, count, existing_items)
                warn = f"大模型 AI 组合失败，已改用红狐真实爆款 + 本地模板生成（仍消耗红狐积分）：{e}"
        else:
            # 红狐没拿到数据，才回退纯本地矩阵
            warn = "红狐真实爆款数据获取失败，已回退本地矩阵（未消耗红狐积分）。"
            loc = _build_local_ideas(seed, min(count, 12))
            raw_ideas = loc.get("ideas", [])
            source = "local"

    if not raw_ideas:
        loc = _build_local_ideas(seed, min(count, 12))
        raw_ideas = loc.get("ideas", [])
        source = "local"
        if warn is None:
            warn = "未生成有效选题，已回退本地矩阵。"

    # 富化评分 + 语义去重 + 排序
    kept = _finalize_candidates(raw_ideas, existing_items, hot_title_strs, api_key=api_key, base_url=base_url, model=model)
    # 新草稿入库
    new_drafts = [it for it in kept if it.get("status") == "draft"]
    if new_drafts:
        existing_items.extend(new_drafts)
        save_idea_bank(existing_items)
    # 返回排序后的 Top count
    top = kept[:count]
    return {
        "seed": seed,
        "count": len(top),
        "themes": themes,
        "ideas": top,
        "source": source,
        "hot_titles": hot_title_strs[:10],
        "bank_total": len(existing_items),
        "bank_new": len(new_drafts),
        "warn": warn,
    }


def _build_local_ideas(seed="", count=6):
    """纯本地矩阵选题（原逻辑，抽出来供 fallback 复用）。"""
    import random
    seed = (seed or "").strip()
    count = max(1, min(12, int(count) if str(count).isdigit() else 6))

    candidates = []
    for theme, scenes in TOPIC_MATRIX.items():
        for scene in scenes:
            candidates.append((theme, scene))

    filtered = candidates
    if seed:
        # 优先匹配主题，再匹配场景
        filtered = [c for c in candidates if seed in c[0] or seed in c[1]]
    if not filtered:
        filtered = candidates

    random.seed()
    chosen = random.sample(filtered, min(count, len(filtered)))

    ideas = []
    used_cuts = set()
    used_quotes = set()
    for theme, scene in chosen:
        cuts = CUT_TEMPLATES.get(theme, ["我曾经也在这个问题上摔过跟头，后来才明白纳瓦尔那句话不是鸡汤。"])

        # 用模板化金句库生成多样化、低重复的核心金句
        quote_pool = generate_local_quotes(theme, topic=scene, count=4, used=used_quotes)
        quote = random.choice(quote_pool) if quote_pool else "把注意力收回来，是你能给自己最好的投资。"

        available_cuts = [c for c in cuts if c not in used_cuts]
        cut = random.choice(available_cuts if available_cuts else cuts)
        used_cuts.add(cut)

        # 根据场景关键词推荐语气
        voice_map = [
            (("职场", "金钱", "账单", "审计", "数据", "算账"), "算账拆解型"),
            (("焦虑", "害怕", "迷茫", "压力"), "清醒陪伴型"),
            (("后悔", "遗憾", "温柔", "陪伴", "成长"), "温柔坚定型"),
            (("消费", "陷阱", "上瘾", "收割", "骗局"), "反骨警示型"),
            (("选择", "对比", "经历", "故事", "朋友"), "故事共鸣型"),
            (("错误", "真相", "本质", "扎心"), "观点刺穿型"),
        ]
        voice = "清醒陪伴型"
        for keys, v in voice_map:
            if any(k in scene for k in keys):
                voice = v
                break

        # 根据场景关键词推荐见解角度
        insight_map = [
            (("金钱", "账单", "审计", "数据", "算账"), "算账视角"),
            (("陷阱", "错误", "设计", "收割", "骗局"), "身份重构"),
            (("选择", "对比", "决定"), "二元对立"),
            (("经历", "故事", "朋友", "一个月", "三年"), "亲身经历"),
            (("女生", "女性", "姐妹"), "女性专属"),
        ]
        insight = "反常识"
        for keys, ins in insight_map:
            if any(k in scene for k in keys):
                insight = ins
                break

        tags = generate_tags(theme, insight)

        ideas.append({
            "theme": theme,
            "scene": scene,
            "topic": scene,
            "naval_quote": quote,
            "cut_template": cut,
            "recommended_voice": voice,
            "recommended_insight": insight,
            "tags": tags,
        })

    return {
        "seed": seed,
        "count": len(ideas),
        "themes": list(TOPIC_MATRIX.keys()),
        "ideas": ideas,
        "source": "local",
    }


# ===================== 选题库：累积、去重、复用 =====================

def ensure_data_dir():
    os.makedirs(os.path.dirname(IDEA_BANK_PATH), exist_ok=True)


def load_idea_bank():
    """读取本地选题库。"""
    ensure_data_dir()
    if not os.path.exists(IDEA_BANK_PATH):
        return {"version": 1, "items": [], "last_updated": ""}
    try:
        with open(IDEA_BANK_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "items" not in data:
            return {"version": 1, "items": [], "last_updated": ""}
        # 归一化旧数据：补 id/status/scores/composite/reason，保证可排序可推荐
        items = data.get("items", [])
        used_for_norm = [i for i in items if i.get("status") in ("used", "archived")]
        data["items"] = [normalize_idea_item(it, used_items=used_for_norm) for it in items]
        return data
    except Exception:
        return {"version": 1, "items": [], "last_updated": ""}


def save_idea_bank(items):
    """保存选题库（items 去重后写入）。"""
    ensure_data_dir()
    data = {
        "version": 1,
        "items": items,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(IDEA_BANK_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _normalize_for_dedupe(text):
    """用于去重的简化文本。"""
    if not text:
        return ""
    t = str(text).lower().strip()
    # 去掉常见无意义词、标点、空格
    for ch in '#，。！？、；："\'()[]{}【】':
        t = t.replace(ch, "")
    t = re.sub(r"\s+", "", t)
    return t


def is_similar_idea(a, b):
    """判断两个选题是否相似（防止重复入库）。仅按「主题(topic)」去重——
    同一场景不同角度的选题视为不同内容，允许都进入选题库，保证批量生产的产量。"""
    a_topic = _normalize_for_dedupe(a.get("topic", ""))
    b_topic = _normalize_for_dedupe(b.get("topic", ""))
    if not a_topic or not b_topic:
        return False
    # 精确相同
    if a_topic == b_topic:
        return True
    # 较长且互相包含（近似同一选题）
    if len(a_topic) >= 6 and len(b_topic) >= 6 and (a_topic in b_topic or b_topic in a_topic):
        return True
    return False


def dedupe_ideas(new_items, existing_items):
    """把 new_items 中与 existing_items 重复的去掉。"""
    out = []
    for it in new_items:
        if not any(is_similar_idea(it, old) for old in existing_items + out):
            out.append(it)
    return out


# ===================== 选题评分：让选题可量化、可解释、不跑偏 =====================
# 综合分 = 0.35·热度 + 0.40·纳瓦尔契合 + 0.25·场景 − 0.30·重复惩罚
# 设计原则：fit（纳瓦尔契合）权重最高 → 锁死"纳瓦尔思想 × 女性成长"定位。
SCORE_WEIGHTS = {"heat": 0.35, "fit": 0.40, "scene": 0.25, "repeat": 0.30}

# 女性成长场景词库（用于衡量选题的"女性向"程度）
SCENE_LEXICON = (
    "女生 女性 姐妹 职场 副业 消费 关系 社交 边界 焦虑 内耗 成长 年龄 时间贫困 "
    "睡眠 自我 婚姻 情感 朋友 约会 独居 宝妈 实习生 备孕 离职 攒钱 搞钱 穿搭 护肤 "
    "情绪 敏感 讨好 自卑 自律 自由 副业 一人公司 上岸 备考 考研 考公 整顿 觉醒"
).split()


def _jaccard(a, b):
    """两个字符串的字符/词集合相似度（用于语义级近似去重）。"""
    sa = set(_normalize_for_dedupe(a))
    sb = set(_normalize_for_dedupe(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _infer_naval_themes(topic, scene):
    """从选题文本推断最相关的 1-3 个纳瓦尔主题（用于解释、评分与生成连贯性）。"""
    text = f"{topic} {scene}"
    scores = {}
    for theme, kws in QUOTE_THEME_KEYWORDS.items():
        score = 0
        for kw in kws:
            if kw and kw in text:
                score += 1 + min(len(kw) / 10, 0.5)
        if score:
            scores[theme] = score
    if not scores:
        return ["注意力"]
    sorted_themes = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [theme for theme, _ in sorted_themes[:3]]


def _score_fit(theme, topic, scene):
    """纳瓦尔契合度：选题文本能命中几个纳瓦尔主题关键词。0.3 起底，命中越多越高。"""
    text = f"{topic} {scene}"
    matched = [t for t, kws in QUOTE_THEME_KEYWORDS.items() if any(kw and kw in text for kw in kws)]
    # 若选题系统传来的 theme 是真实纳瓦尔主题（在 NAVAL_QUOTES 中），也计入命中
    real_theme = None
    if theme in NAVAL_QUOTES:
        real_theme = theme
    elif theme:
        for k in NAVAL_QUOTES:
            if theme in k or k.startswith(theme):
                real_theme = k
                break
    if real_theme and real_theme not in matched:
        matched = matched + [real_theme]
    return round(min(1.0, 0.3 + 0.3 * len(matched)), 3)


def _score_scene(topic, scene):
    """场景女性向程度：命中场景词库越多越贴合。"""
    text = f"{topic} {scene}"
    hits = sum(1 for w in SCENE_LEXICON if w in text)
    return round(min(1.0, 0.4 + 0.3 * hits), 3)


def _repeat_penalty(cand, used_items):
    """与已发布/已归档选题的重复度（0~0.8）。强相似直接判 1.0（几乎必被过滤）。"""
    topic_a = cand.get("topic", "")
    maxp = 0.0
    for old in used_items:
        if old.get("status") not in ("used", "archived"):
            continue
        if is_similar_idea(cand, old):
            return 1.0
        j = _jaccard(topic_a, old.get("topic", ""))
        if j > maxp:
            maxp = j
    return round(min(0.8, maxp), 3)


def score_idea(it, hot_title_strs=None, used_items=None):
    """对单条选题打分，返回 {heat, fit, scene, repeat_penalty}。"""
    hot_title_strs = hot_title_strs or []
    used_items = used_items or []
    src = it.get("source", "")
    # 热度：真实爆款来源给高，本地中性；若选题文本命中爆款标题再小幅加成
    if "redfox" in src:
        heat = 0.8
        text = f"{it.get('topic','')} {it.get('scene','')}"
        if any(t and t in text for t in hot_title_strs):
            heat = min(1.0, heat + 0.1)
    else:
        heat = 0.5
    fit = _score_fit(it.get("theme", ""), it.get("topic", ""), it.get("scene", ""))
    scene = _score_scene(it.get("topic", ""), it.get("scene", ""))
    rep = _repeat_penalty(it, used_items)
    return {"heat": round(heat, 3), "fit": fit, "scene": scene, "repeat_penalty": rep}


def weighted_composite(scores):
    w = SCORE_WEIGHTS
    comp = w["heat"] * scores["heat"] + w["fit"] * scores["fit"] + w["scene"] * scores["scene"] - w["repeat"] * scores["repeat_penalty"]
    return round(max(0.0, min(1.0, comp)), 3)


def explain_score(scores, naval_theme):
    parts = []
    if scores["heat"] >= 0.7:
        parts.append("贴近近期真实爆款")
    elif scores["heat"] >= 0.5:
        parts.append("热度中等")
    else:
        parts.append("热度偏低")
    if scores["fit"] >= 0.7:
        parts.append(f"高度契合约瓦尔「{naval_theme}」内核")
    elif scores["fit"] >= 0.5:
        parts.append(f"契合约瓦尔「{naval_theme}」")
    else:
        parts.append("纳瓦尔内核偏弱")
    if scores["scene"] >= 0.6:
        parts.append("女性向场景明确")
    else:
        parts.append("场景可更聚焦女性")
    if scores["repeat_penalty"] >= 0.5:
        parts.append("与已发布选题接近，已降权")
    return " · ".join(parts)


def _enrich_idea(it, hot_title_strs=None, used_items=None):
    """给单条选题补上评分、综合分、推荐理由、状态等字段。"""
    naval_themes = _infer_naval_themes(it.get("topic", ""), it.get("scene", ""))
    naval_theme = naval_themes[0]
    scores = score_idea(it, hot_title_strs=hot_title_strs, used_items=used_items)
    composite = weighted_composite(scores)
    reason = explain_score(scores, naval_theme)
    it["naval_themes"] = naval_themes
    it["naval_theme"] = naval_theme
    it["scores"] = scores
    it["composite"] = composite
    it["reason"] = reason
    it.setdefault("status", "draft")
    it.setdefault("used_at", None)
    it.setdefault("id", uuid.uuid4().hex[:12])
    it.setdefault("created_at", datetime.now().strftime("%Y-%m-%d"))
    return it


def normalize_idea_item(it, used_items=None):
    """读取时归一化旧数据：补 id/status/scores/composite/reason/naval_themes。
    旧选题没有分数或主题字段为空/写死旧值时，用当前文本反推，保证选题库可排序、可推荐。"""
    it = dict(it)
    it.setdefault("id", uuid.uuid4().hex[:12])
    it.setdefault("status", "draft")
    it.setdefault("used_at", None)
    it.setdefault("created_at", datetime.now().strftime("%Y-%m-%d"))
    needs_enrich = (
        "composite" not in it or "scores" not in it or "naval_themes" not in it
        or not it.get("naval_theme") or it.get("naval_theme") == "红狐实时"
        or not it.get("theme") or it.get("theme") == "红狐实时"
    )
    if needs_enrich:
        _enrich_idea(it, used_items=used_items or [])
    return it


def ai_semantic_dedupe(candidates, used_topics, api_key, base_url, model, timeout=20):
    """AI 语义判重（第二档）：一次性把候选与已发布选题交给模型判定哪些重复。
    返回被判定为重复的候选索引集合。失败/无 Key 时返回空集合（退回关键词去重）。"""
    if not api_key or not used_topics or not candidates:
        return set()
    used_block = "\n".join(f"- {t}" for t in used_topics[:40])
    cand_block = "\n".join(f"{i}. {c.get('topic','')}" for i, c in enumerate(candidates))
    sys_p = "你是选题去重助手。给定一批新选题和一批已发布选题，请判断哪些新选题在含义上与已发布选题重复（同主题同角度即算重复）。"
    user_p = f"【已发布选题】\n{used_block}\n\n【新选题】\n{cand_block}\n\n请只返回重复的新选题编号（0-based），用 JSON 数组，如 [0,3]。不要额外文字。"
    try:
        raw = call_openai_compatible(sys_p, user_p, api_key, base_url, model, timeout=timeout)
        raw = strip_code(raw)
        arr = json.loads(raw)
        if isinstance(arr, list):
            return set(int(x) for x in arr if isinstance(x, (int, float)))
    except Exception:
        pass
    return set()


def _match_theme_to_hot_title(hot_title):
    """根据爆款标题中的关键词匹配最相关的纳瓦尔主题。"""
    title = str(hot_title or "").lower()
    scores = {}
    for theme, scenes in TOPIC_MATRIX.items():
        score = 0
        # 主题名命中
        if theme in title:
            score += 3
        # 场景关键词命中
        for scene in scenes:
            for word in scene.lower().split():
                if len(word) >= 2 and word in title:
                    score += 1
        scores[theme] = score
    # 特殊关键词加权
    keyword_bonus = {
        "注意力": "注意力",
        "金钱": "财富", "钱": "财富", "搞钱": "财富", "存钱": "财富", "理财": "财富",
        "决策": "判断力", "选择": "判断力",
        "幸福": "幸福", "快乐": "幸福",
        "杠杆": "杠杆", "自媒体": "杠杆", "一人公司": "杠杆",
        "专长": "专长", "擅长": "专长", "技能": "专长",
        "复利": "复利", "长期": "复利", "坚持": "复利",
        "自由": "自由", "财务自由": "自由", "时间自由": "自由",
        "学习": "学习", "读书": "学习", "知识": "学习",
        "健康": "健康", "精力": "健康", "睡眠": "健康",
        "时间": "时间", "时间管理": "时间",
        "产品化": "产品化", "ip": "产品化", "个人品牌": "产品化",
        "关系": "关系", "社交": "关系", "边界": "关系",
    }
    for kw, theme in keyword_bonus.items():
        if kw in title:
            scores[theme] = scores.get(theme, 0) + 2
    if not scores:
        return "注意力"
    best = max(scores, key=scores.get)
    if scores[best] <= 0:
        return "注意力"
    return best


def _voice_for_scene(scene):
    voice_map = [
        (("职场", "金钱", "账单", "审计", "数据", "算账"), "算账拆解型"),
        (("焦虑", "害怕", "迷茫", "压力"), "清醒陪伴型"),
        (("后悔", "遗憾", "温柔", "陪伴", "成长"), "温柔坚定型"),
        (("消费", "陷阱", "上瘾", "收割", "骗局"), "反骨警示型"),
        (("选择", "对比", "经历", "故事", "朋友"), "故事共鸣型"),
        (("错误", "真相", "本质", "扎心"), "观点刺穿型"),
    ]
    for keys, v in voice_map:
        if any(k in scene for k in keys):
            return v
    return "清醒陪伴型"


def _insight_for_scene(scene):
    insight_map = [
        (("金钱", "账单", "审计", "数据", "算账"), "算账视角"),
        (("陷阱", "错误", "设计", "收割", "骗局"), "身份重构"),
        (("选择", "对比", "决定"), "二元对立"),
        (("经历", "故事", "朋友", "一个月", "三年"), "亲身经历"),
        (("女生", "女性", "姐妹"), "女性专属"),
    ]
    for keys, ins in insight_map:
        if any(k in scene for k in keys):
            return ins
    return "反常识"


def _rotate_combinations(theme, scene, hot_title, idx, used_cuts=None, used_quotes=None):
    """为主题/场景/爆款标题生成多种组合，避免重复。
    优先用场景匹配的语气/角度，再用 idx 扰动 voice/insight，让同一批次更富变化。
    金句使用模板化素材库 + 本批次去重，避免 3 条固定模板轮询。"""
    import random
    cuts = CUT_TEMPLATES.get(theme, ["我曾经也在这个问题上摔过跟头，后来才明白纳瓦尔那句话不是鸡汤。"])
    voices = ["清醒陪伴型", "反骨警示型", "温柔坚定型", "算账拆解型", "故事共鸣型", "观点刺穿型"]
    insights = ["反常识", "女性专属", "算账视角", "亲身经历", "二元对立", "身份重构"]

    # 随机选未用过的切口，没有则用全部池子
    used = set(used_cuts or [])
    available_cuts = [c for c in cuts if c not in used]
    cut_pool = available_cuts if available_cuts else cuts
    cut = random.choice(cut_pool)

    # 金句从模板化素材库生成，跨本批次去重
    quote_pool = generate_local_quotes(theme, topic=scene, count=4, used=used_quotes)
    quote = random.choice(quote_pool) if quote_pool else "把注意力收回来，是你能给自己最好的投资。"

    # 基调由场景决定，但每 3 个做一次扰动，保证变化
    base_voice = _voice_for_scene(scene)
    base_insight = _insight_for_scene(scene)
    if idx % 3 == 0:
        voice = base_voice
        insight = base_insight
    elif idx % 3 == 1:
        voice = voices[idx % len(voices)]
        insight = base_insight
    else:
        voice = base_voice
        insight = insights[idx % len(insights)]
    return quote, cut, voice, insight


def build_ideas_from_hot_titles(seed, hot_titles, count, existing_items):
    """基于红狐真实爆款标题，批量组合出 count 条不重复选题（不依赖 AI Key）。

    设计思路：一次红狐 search 调用拿到真实爆款标题（消耗积分），
    然后与本地纳瓦尔主题矩阵做笛卡尔组合，通过不同金句/切口/语气/角度
    生成大量差异化选题，满足批量生产需求。
    """
    import random
    random.seed()

    candidates = []
    for theme, scenes in TOPIC_MATRIX.items():
        for scene in scenes:
            candidates.append((theme, scene))

    # 若用户给了种子词，把相关主题排到前面、其余打乱，保证每次调取都能铺满 count 且尽量不重复；
    # 但「不限制总数」——批量生产需要足够多的差异化场景，故 padding 始终覆盖全矩阵（14 主题×多场景）。
    if seed:
        seed_lower = seed.lower()
        matched = [c for c in candidates if seed_lower in c[0].lower() or seed_lower in c[1].lower()]
        if matched:
            rest = [c for c in candidates if c not in matched]
            random.shuffle(rest)
            candidates = matched + rest
    else:
        random.shuffle(candidates)

    ideas = []
    # 只在本批次内去重（避免同一爆款/场景重复出现），不预过滤已入库选题——
    # 否则当选题库很大时，新调取的热门标题若恰好与库中某条相似，就会直接返回 0 条，
    # 让用户误以为红狐没被调用。入库时的 dedupe_ideas 会负责过滤真正重复项。
    seen_topics = set()
    used_cuts = set()
    used_quotes = set()

    # 优先把每个真实爆款标题与最匹配主题组合
    for i, hot in enumerate(hot_titles):
        if len(ideas) >= count:
            break
        title = hot.get("title", "") if isinstance(hot, dict) else str(hot)
        if not title:
            continue
        theme = _match_theme_to_hot_title(title)
        scenes = TOPIC_MATRIX.get(theme, [])
        if not scenes:
            continue
        scene = scenes[i % len(scenes)]
        # 构造选题 topic：把爆款标题改写成纳瓦尔视角
        topic = title
        quote, cut, voice, insight = _rotate_combinations(theme, scene, title, i, used_cuts=used_cuts, used_quotes=used_quotes)
        used_cuts.add(cut)
        tags = generate_tags(theme, insight)
        item = {
            "theme": theme,  # 用真实纳瓦尔主题，便于选题库筛选与后续生成连贯
            "scene": scene,
            "topic": topic,
            "naval_quote": quote,
            "cut_template": cut,
            "recommended_voice": voice,
            "recommended_insight": insight,
            "tags": tags,
            "source": "redfox+ai",
            "hot_title": title,
        }
        norm = _normalize_for_dedupe(topic)
        if norm and norm not in seen_topics:
            seen_topics.add(norm)
            ideas.append(item)

    # 如果爆款标题不够，用本地矩阵继续组合补齐 count
    extra_idx = 0
    while len(ideas) < count and candidates:
        theme, scene = candidates[extra_idx % len(candidates)]
        quote, cut, voice, insight = _rotate_combinations(theme, scene, "", extra_idx, used_cuts=used_cuts, used_quotes=used_quotes)
        used_cuts.add(cut)
        topic = scene
        norm = _normalize_for_dedupe(topic)
        if norm and norm not in seen_topics:
            seen_topics.add(norm)
            ideas.append({
                "theme": theme,
                "scene": scene,
                "topic": topic,
                "naval_quote": quote,
                "cut_template": cut,
                "recommended_voice": voice,
                "recommended_insight": insight,
                "tags": generate_tags(theme, insight),
                "source": "redfox+ai",
            })
        extra_idx += 1
        if extra_idx > count * 5:
            break

    return ideas


# ===================== 质检AI：去AI味 + 风格校验 =====================

QC_SYSTEM = """你是内容质检编辑，负责检查一篇"完整小红书帖子"（含封面标题、详情页标题、核心观点、标签、图文正文）是否达到发布标准。

检查维度：
1) 标题一致性：detail_title 是否等于 cover_title（标题一致性锚点）；
2) 核心观点贯穿：core_viewpoint 是否明确、是否在正文开头出现；
3) 标签分层：tags 是否覆盖"身份/方法论/结果/平台热词"四层、数量≥8；
4) AI味：是否堆砌"首先/其次/总而言之/值得一提的是/在当今社会"等套话；
5) 金句密度：每页是否有可截图传播的句子；
6) 女性向一致性：语气是否平视、陪伴、有共鸣；
7) 人味：是否有真实细节而非空泛道理；
8) 行动召唤：正文是否以可执行的行动指令收尾（认知→行动递进是否完成）；
9) 结构完整：6 段递进是否齐全。

你必须只返回一个 JSON 对象（不要额外文字、不要 markdown 代码块），结构如下：
{
  "score": 88,
  "verdict": "可发布/需修改",
  "title_consistency": true,
  "core_present": true,
  "tags_layering": "达标（覆盖身份/方法论/结果/平台热词四层，共10个）",
  "ai_smell": ["发现的AI套话1", "套话2"],
  "quote_density": "高/中/低",
  "action_cta": true,
  "issues": ["具体问题1", "问题2"],
  "suggestions": ["改写建议1", "建议2"]
}"""


def qc_markdown(markdown, topic, api_key, base_url, model, cover_title="", detail_title="", core_viewpoint="", tags_str=""):
    extra = ""
    if cover_title or detail_title or core_viewpoint or tags_str:
        extra = f"\n【帖子结构字段（用于校验一致性）】\n- cover_title：{cover_title}\n- detail_title：{detail_title}\n- core_viewpoint：{core_viewpoint}\n- tags：{tags_str}"
    user = f"【选题】{topic}\n【待质检正文】\n{markdown}{extra}\n\n请输出质检 JSON。score 为 0-100 的发布质量分（越高越好）。严格只返回 JSON。"
    raw = call_openai_compatible(QC_SYSTEM, user, api_key, base_url, model)
    raw = strip_code(raw)
    try:
        return json.loads(raw)
    except Exception:
        return {"error": "质检解析失败", "raw": raw[:800]}


# ===================== 路由 =====================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def api_config():
    """返回基础配置（不暴露完整 Key）。红狐 Key 以 winreg 权威来源为准，确保 UI 准确显示已配置。"""
    _rf = _resolve_redfox_key()
    return jsonify({
        "openai_base": RUNTIME["openai_base"],
        "openai_key_configured": bool(RUNTIME["openai_key"]),
        "openai_key_masked": mask_key(RUNTIME["openai_key"]),
        "redfox_key_configured": bool(_rf),
        "redfox_key_masked": mask_key(_rf),
        "default_model": RUNTIME["default_model"],
    })


@app.route("/api/config/update", methods=["POST"])
def api_config_update():
    """热更新运行时配置（仅当前进程生效，不写入环境变量文件）。

    关键防护：redfox_api_key / openai_key 收到空值或占位符(ak_test / test.example)时
    一律忽略，保留权威来源(Windows 用户级环境变量 winreg)，避免浏览器自动调用本接口
    把 RUNTIME 的 key 覆盖成空/错误值，导致所有红狐调用鉴权失败、积分不消耗。
    """
    data = request.get_json() or {}
    _BAD = ("ak_test", "test.example", "example.com", "your_", "xxxx", "sk-test", "...", "*****")

    def _safe_key(v):
        v = (v or "").strip()
        if not v:
            return None
        low = v.lower()
        if any(b in low for b in _BAD):
            return None
        return v

    if "redfox_api_key" in data:
        sk = _safe_key(data["redfox_api_key"])
        if sk:
            RUNTIME["redfox_api_key"] = sk
    if "openai_base" in data:
        RUNTIME["openai_base"] = str(data["openai_base"] or "").strip().rstrip("/") or RUNTIME["openai_base"]
    if "openai_key" in data:
        ok = _safe_key(data["openai_key"])
        if ok:
            RUNTIME["openai_key"] = ok
    if "default_model" in data:
        RUNTIME["default_model"] = str(data["default_model"] or "Auto").strip()
    # 落盘到 config.json：保证重启后依然生效（跨设备/跨重启配置持久化，不再依赖 Windows 注册表）
    _CFG["redfox_api_key"] = RUNTIME["redfox_api_key"]
    _CFG["openai_base"] = RUNTIME["openai_base"]
    _CFG["openai_key"] = RUNTIME["openai_key"]
    _CFG["default_model"] = RUNTIME["default_model"]
    save_cfg_file(_CFG)
    return jsonify({
        "ok": True,
        "openai_base": RUNTIME["openai_base"],
        "openai_key_configured": bool(RUNTIME["openai_key"]),
        "openai_key_masked": mask_key(RUNTIME["openai_key"]),
        "redfox_key_configured": bool(_resolve_redfox_key()),
        "redfox_key_masked": mask_key(_resolve_redfox_key()),
        "default_model": RUNTIME["default_model"],
    })


@app.route("/api/call-logs", methods=["GET"])
def api_call_logs():
    """返回本地 API 调用日志（最近 N 条，默认 50），用于排查生产环境调用记录缺失问题。

    参数：?limit=50&offset=0
    """
    try:
        limit = min(200, max(1, int(request.args.get("limit", 50))))
        offset = max(0, int(request.args.get("offset", 0)))
    except Exception:
        limit, offset = 50, 0
    logs = []
    if os.path.exists(API_CALL_LOG):
        try:
            with open(API_CALL_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        logs.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            pass
    total = len(logs)
    # 时间倒序，取最近 limit 条
    logs.reverse()
    page = logs[offset:offset + limit]
    return jsonify({"total": total, "limit": limit, "offset": offset, "logs": page})


@app.route("/api/models", methods=["GET"])
def api_models():
    """列出可用模型"""
    if not RUNTIME["openai_key"]:
        return jsonify({"error": "未配置 OpenAI API Key", "models": []})
    try:
        url = f"{RUNTIME['openai_base']}/models"
        headers = {"Authorization": "Bearer " + RUNTIME["openai_key"]}
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            data = r.json()
            models = [m.get("id") for m in data.get("data", [])]
            return jsonify({"models": models})
        return jsonify({"error": f"HTTP {r.status_code}", "models": []})
    except Exception as e:
        return jsonify({"error": str(e), "models": []})


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json() or {}
    keyword = data.get("keyword", "").strip()
    if not keyword:
        return jsonify({"error": "关键词不能为空"}), 400
    redfox_api_key = (data.get("redfox_api_key") or "").strip() or RUNTIME["redfox_api_key"]
    result = run_search(keyword, redfox_api_key=redfox_api_key)
    return jsonify(result)


@app.route("/api/research", methods=["POST"])
def api_research():
    """调研AI：聚合多个红狐 skill + OpenAI 深度分析 -> 丰富调研简报 JSON"""
    data = request.get_json() or {}
    topic = data.get("topic", "").strip()
    if not topic:
        return jsonify({"error": "选题不能为空"}), 400
    api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
    base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
    model = (data.get("model") or "").strip() or RUNTIME["default_model"]
    redfox_api_key = (data.get("redfox_api_key") or "").strip() or RUNTIME["redfox_api_key"]
    if not api_key:
        return jsonify({"error": "调研需要大模型 API Key。请先在上方 API 配置里填写。"}), 400

    # 1) 多 skill 聚合
    insights = gather_redfox_insights(topic, redfox_api_key=redfox_api_key)
    search = insights.get("search") or {}
    items = search.get("items", []) if isinstance(search, dict) else []
    hot_titles = [it["title"] for it in items if it.get("title")]

    extra = {}
    if isinstance(insights.get("covers"), dict):
        extra["covers"] = insights["covers"]
    if isinstance(insights.get("trends"), dict):
        extra["trends"] = insights["trends"]
    if isinstance(insights.get("weekly"), dict):
        extra["weekly"] = insights["weekly"]
    rank_res = insights.get("rank") or {}
    if isinstance(rank_res, dict):
        extra["top_accounts"] = rank_res
    similar_res = insights.get("similar") or {}
    if isinstance(similar_res, dict):
        extra["similar_accounts"] = similar_res

    # 2) AI 深度分析
    brief = research_topic(topic, hot_titles, api_key, base_url, model, extra_insights=extra)
    brief["_search_count"] = len(items)
    brief["_hot_titles_raw"] = hot_titles
    brief["_redfox_sources"] = {k: ("ok" if not isinstance(v, dict) or not v.get("error") else v.get("error"))
                                  for k, v in insights.items()}
    return jsonify(brief)


@app.route("/api/distill", methods=["POST"])
def api_distill():
    """蒸馏AI：调研简报 -> 差异化内容策略 JSON"""
    data = request.get_json() or {}
    brief = data.get("brief") or {}
    if not brief:
        return jsonify({"error": "请先完成调研（/api/research）"}), 400
    api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
    base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
    model = (data.get("model") or "").strip() or RUNTIME["default_model"]
    if not api_key:
        return jsonify({"error": "蒸馏需要大模型 API Key。请先在上方 API 配置里填写。"}), 400

    strategy = distill_brief(brief, api_key, base_url, model)
    return jsonify(strategy)


@app.route("/api/ideate", methods=["POST"])
def api_ideate():
    """选题工坊：输入种子词，输出系统化选题包（含金句/切口/语气/角度/标签）。
    可选 use_redfox=true 时，先调红狐拉真实爆款，再让 AI 结合纳瓦尔主题反向组合。"""
    data = request.get_json() or {}
    seed = data.get("seed", "").strip()
    count = data.get("count", 6)
    use_redfox = bool(data.get("use_redfox", True))
    api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
    base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
    model = (data.get("model") or "").strip() or RUNTIME["default_model"]
    redfox_api_key = (data.get("redfox_api_key") or "").strip() or RUNTIME["redfox_api_key"]
    result = generate_ideas(seed, count, use_redfox=use_redfox, api_key=api_key, base_url=base_url, model=model, redfox_api_key=redfox_api_key)
    return jsonify(result)


@app.route("/api/recommend", methods=["POST"])
def api_recommend():
    """智能选题：从选题库草稿中按「主题多样性 + 综合分 + 新鲜度」推荐 Top N，草稿不足时自动补生成。"""
    data = request.get_json() or {}
    seed = (data.get("seed", "") or "").strip()
    try:
        n = max(1, int(data.get("count", 10)))
    except Exception:
        n = 10
    use_redfox = bool(data.get("use_redfox", True))
    api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
    base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
    model = (data.get("model") or "").strip() or RUNTIME["default_model"]
    redfox_api_key = (data.get("redfox_api_key") or "").strip() or RUNTIME["redfox_api_key"]

    def _filter_by_seed(items, seed):
        if not seed:
            return items
        sd = seed.lower()
        # 种子词扩展：命中哪些纳瓦尔主题（如"金钱"→财富/欲望/消费等）
        related_themes = set()
        for theme, kws in NAVAL_TOPIC_KEYWORDS.items():
            if any(kw.lower() in sd for kw in kws):
                related_themes.add(theme)
        def _match(item):
            text = (item.get("topic", "") + item.get("scene", "") + item.get("theme", "")).lower()
            naval_themes = [t.lower() for t in item.get("naval_themes", [])]
            return (
                sd in text
                or sd in (",".join(item.get("naval_themes", []))).lower()
                or (item.get("naval_theme") or "").lower() in related_themes
                or any(t in related_themes for t in naval_themes)
            )
        return [d for d in items if _match(d)]

    bank = load_idea_bank()
    all_items = bank.get("items", [])
    drafts = [i for i in all_items if i.get("status", "draft") == "draft"]
    drafts = _filter_by_seed(drafts, seed)

    # 草稿不足则自动补生成
    if len(drafts) < n:
        need = n - len(drafts)
        generate_ideas(seed, count=max(need * 3, 12), use_redfox=use_redfox,
                       api_key=api_key, base_url=base_url, model=model, redfox_api_key=redfox_api_key)
        bank = load_idea_bank()
        drafts = [i for i in bank.get("items", []) if i.get("status", "draft") == "draft"]
        drafts = _filter_by_seed(drafts, seed)

    # 排序：综合分降序，同分按 created_at 新优先
    # 排序：先按种子词相关度，再按综合分降序，同分按创建时间新优先
    seed_lower = seed.lower() if seed else ""
    # 预计算：种子词命中哪些纳瓦尔主题
    seed_matched_themes = set()
    if seed_lower:
        for theme, kws in NAVAL_TOPIC_KEYWORDS.items():
            if any(kw.lower() in seed_lower for kw in kws):
                seed_matched_themes.add(theme)

    def _relevance_score(i):
        if not seed_lower:
            return 0
        naval_theme = (i.get("naval_theme") or "").lower()
        # 种子词直接命中 naval_theme 最高优先级
        if seed_lower == naval_theme:
            return 4
        if naval_theme in seed_matched_themes:
            return 3
        if any((t or "").lower() in seed_matched_themes for t in i.get("naval_themes", [])):
            return 2
        # 命中 topic/scene
        text = f"{i.get('topic','')} {i.get('scene','')}"
        if seed_lower in text.lower():
            return 1
        return 0

    drafts.sort(key=lambda i: (_relevance_score(i), i.get("composite", 0), i.get("created_at", "")), reverse=True)

    # 多样性挑选：每个纳瓦尔主题最多 2 条，先保证覆盖不同主题，再按综合分补齐
    theme_quota = {}
    selected = []
    used_ids = set()
    for d in drafts:
        th = d.get("naval_theme") or d.get("theme") or "其他"
        if theme_quota.get(th, 0) >= 2:
            continue
        if d.get("id") in used_ids:
            continue
        selected.append(d)
        used_ids.add(d["id"])
        theme_quota[th] = theme_quota.get(th, 0) + 1
        if len(selected) >= n:
            break

    # 兜底：如果多样性挑选不足 n，按综合分补齐（但仍跳过已用）
    if len(selected) < n:
        for d in drafts:
            if d.get("id") not in used_ids:
                selected.append(d)
                used_ids.add(d["id"])
            if len(selected) >= n:
                break

    return jsonify({
        "seed": seed,
        "count": len(selected),
        "ideas": selected,
        "source": "recommend",
        "bank_total": len(all_items),
        "draft_total": len([i for i in bank.get("items", []) if i.get("status", "draft") == "draft"]),
    })


@app.route("/api/idea-bank/mark-used", methods=["POST"])
def api_idea_bank_mark_used():
    """把某条选题标记为已用（被拿去生成笔记），参与闭环去重，不再被智能推荐。"""
    data = request.get_json() or {}
    topic = (data.get("topic") or "").strip()
    item_id = (data.get("id") or "").strip()
    if not topic and not item_id:
        return jsonify({"ok": False, "error": "缺少 topic 或 id"}), 400
    bank = load_idea_bank()
    items = bank.get("items", [])
    matched = 0
    for it in items:
        if (item_id and it.get("id") == item_id) or (topic and _normalize_for_dedupe(it.get("topic", "")) == _normalize_for_dedupe(topic)):
            if it.get("status") != "used":
                it["status"] = "used"
                it["used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                matched += 1
    save_idea_bank(items)
    return jsonify({"ok": True, "matched": matched, "draft_total": len([i for i in items if i.get("status", "draft") == "draft"])})


@app.route("/api/idea-bank/batch", methods=["POST"])
def api_idea_bank_batch():
    """批量操作选题库：used/draft/archived/delete。
    body: {ids: [id1, id2, ...], action: "used|draft|archived|delete"}
    """
    data = request.get_json() or {}
    ids = data.get("ids") or []
    action = (data.get("action") or "").strip().lower()
    if not ids or not isinstance(ids, list):
        return jsonify({"ok": False, "error": "缺少 ids 列表"}), 400
    if action not in {"used", "draft", "archived", "delete"}:
        return jsonify({"ok": False, "error": "action 必须是 used/draft/archived/delete 之一"}), 400
    bank = load_idea_bank()
    items = bank.get("items", [])
    id_set = set(str(i) for i in ids if i)
    matched = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if action == "delete":
        new_items = [it for it in items if str(it.get("id", "")) not in id_set]
        matched = len(items) - len(new_items)
        items = new_items
    else:
        for it in items:
            if str(it.get("id", "")) in id_set:
                it["status"] = action
                if action == "used":
                    it["used_at"] = now
                matched += 1
    save_idea_bank(items)
    counts = {"all": len(items), "draft": 0, "used": 0, "archived": 0}
    for it in items:
        st = it.get("status", "draft")
        if st in counts:
            counts[st] += 1
    return jsonify({"ok": True, "matched": matched, "counts": counts})


@app.route("/api/idea-bank", methods=["GET"])
def api_idea_bank_get():
    """获取本地选题库（支持按主题/种子词筛选），并返回各状态数量用于前端标签角标。"""
    bank = load_idea_bank()
    items = bank.get("items", [])
    seed = request.args.get("seed", "").strip().lower()
    if seed:
        items = [it for it in items if seed in (it.get("topic") or "").lower()
                 or seed in (it.get("theme") or "").lower()
                 or seed in (it.get("scene") or "").lower()]
    counts = {
        "all": len(bank.get("items", [])),
        "draft": 0,
        "used": 0,
        "archived": 0,
    }
    for it in bank.get("items", []):
        st = it.get("status", "draft")
        if st in counts:
            counts[st] += 1
    return jsonify({
        "total": len(bank.get("items", [])),
        "filtered": len(items),
        "items": items,
        "counts": counts,
        "last_updated": bank.get("last_updated", ""),
    })


@app.route("/api/idea-bank", methods=["DELETE"])
def api_idea_bank_clear():
    """清空本地选题库。"""
    save_idea_bank([])
    return jsonify({"ok": True, "total": 0})


@app.route("/api/idea-bank/export", methods=["GET"])
def api_idea_bank_export():
    """导出选题库为 Markdown。"""
    bank = load_idea_bank()
    items = bank.get("items", [])
    lines = ["# 纳瓦尔女性向选题库", f"> 共 {len(items)} 条 | 最后更新：{bank.get('last_updated', '')}", ""]
    for i, it in enumerate(items, 1):
        lines.append(f"## {i}. {it.get('topic', '')}")
        lines.append(f"- 主题：{it.get('theme', '')} / {it.get('scene', '')}")
        lines.append(f"- 金句：{it.get('naval_quote', '')}")
        lines.append(f"- 切口：{it.get('cut_template', '')}")
        lines.append(f"- 语气：{it.get('recommended_voice', '')} · 角度：{it.get('recommended_insight', '')}")
        lines.append(f"- 标签：{it.get('tags', '')}")
        lines.append("")
    return "\n".join(lines), 200, {"Content-Type": "text/markdown; charset=utf-8"}


@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.get_json() or {}
    topic = data.get("topic", "").strip()
    angle = data.get("angle", "").strip()
    hook = data.get("hook", "").strip()
    voice = data.get("voice", "清醒陪伴型")
    insight = data.get("insight", "反常识")
    cut = data.get("cut", "").strip()
    theme = data.get("theme", "").strip() or data.get("naval_theme", "").strip()
    mode = data.get("mode", "ai")
    real_notes = data.get("real_notes", []) or []
    brief = data.get("brief") or None
    strategy = data.get("strategy") or None

    if not topic:
        return jsonify({"error": "选题不能为空"}), 400
    # hook（纳瓦尔金句）允许为空：为空时由 match_naval_thoughts 兜底注入纳瓦尔思想，
    # 保证即使不填金句也能生成有纳瓦尔内核的内容。

    if mode == "ai":
        api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
        base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
        model = (data.get("model") or "").strip() or RUNTIME["default_model"]
        if not api_key:
            return jsonify({"error": "AI 模式需要大模型 API Key。请先在上方 API 配置里填写。"}), 400
        if not base_url:
            return jsonify({"error": "AI 模式需要 Base URL"}), 400
        # 默认用红狐真实爆款（search+trends+covers）为生成提供灵感，使每次生成都消耗红狐积分
        auto_redfox = bool(data.get("auto_redfox", True))
        grounded = False
        if auto_redfox:
            redfox_api_key = (data.get("redfox_api_key") or "").strip() or RUNTIME["redfox_api_key"]
            try:
                extra = gather_generation_inspiration(topic, redfox_api_key=redfox_api_key)
                if extra:
                    real_notes = (real_notes or []) + extra
                    grounded = True
            except Exception:
                pass
        result = generate_with_ai(topic, hook, voice, insight, cut, real_notes, api_key, base_url, model, brief, strategy, theme=theme)
        result["_redfox_grounded"] = grounded
        _title = result.get("cover_title") or result.get("title") or ""
        result["title_length"] = len(_title)
        result["title_too_long"] = len(_title) > TITLE_MAX_LEN
        return jsonify(result)
    else:
        result = generate_long_image_markdown(topic, angle, hook, voice, insight, cut, theme=theme)
        # 模板模式补齐五要素结构化字段，保持前后端一致
        ttags = result.get("tags", "")
        # body = 内页正文（带 ## 分页标题），不含标题/核心观点/末尾标签
        # 标签只在完整 markdown 出现一次
        body_lines = []
        for p in result.get("pages", []):
            if p.get("type") != "page":
                continue
            if p.get("title"):
                body_lines.append(f"## {p['title']}")
                body_lines.append("")
            body_lines.append(p.get("body", "").strip())
            body_lines.append("")
        body_no_tags = "\n".join(body_lines).strip()
        result["cover_title"] = result.get("title", topic)
        result["detail_title"] = result.get("title", topic)
        result["core_viewpoint"] = result.get("core_viewpoint", "")
        result["tags"] = ttags
        result["tags_list"] = re.findall(r"#\S+", ttags) if ttags else []
        result["body"] = body_no_tags
        _title = result.get("cover_title") or result.get("title") or ""
        result["title_length"] = len(_title)
        result["title_too_long"] = len(_title) > TITLE_MAX_LEN
        return jsonify(result)


@app.route("/api/generate/batch", methods=["POST"])
def api_generate_batch():
    """批量生成：按传入列表顺序逐条生成，支持 ai/template 两种模式。
    body: {
        items: [
            {topic, hook, voice, insight, cut, angle, theme, naval_theme, brief, strategy, real_notes, ...},
            ...
        ],
        mode: "ai" | "template",
        mark_used: true,           // 生成后是否把选题标为已用
        auto_redfox: true,         // AI 模式是否调用红狐灵感
        api_key, base_url, model   // AI 模式可覆盖
    }
    """
    data = request.get_json() or {}
    items = data.get("items") or []
    if not items or not isinstance(items, list):
        return jsonify({"error": "缺少 items 列表"}), 400

    mode = (data.get("mode") or "template").strip().lower()
    mark_used = bool(data.get("mark_used", True))
    auto_redfox = bool(data.get("auto_redfox", True))
    api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
    base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
    model = (data.get("model") or "").strip() or RUNTIME["default_model"]

    if mode == "ai" and (not api_key or not base_url):
        return jsonify({"error": "AI 批量模式需要大模型 API Key 和 Base URL"}), 400

    bank = load_idea_bank()
    bank_items = bank.get("items", [])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    generated_ids = set()
    dirty = False
    results = []

    for idx, it in enumerate(items, 1):
        topic = (it.get("topic") or "").strip()
        if not topic:
            results.append({"ok": False, "idx": idx, "error": "选题不能为空", "topic": topic})
            continue
        try:
            hook = (it.get("hook") or "").strip()
            voice = it.get("voice", "清醒陪伴型")
            insight = it.get("insight", "反常识")
            cut = (it.get("cut") or it.get("cut_template") or "").strip()
            angle = (it.get("angle") or "").strip()
            theme = (it.get("theme") or it.get("naval_theme") or "").strip()
            brief = it.get("brief") or None
            strategy = it.get("strategy") or None
            real_notes = it.get("real_notes") or []

            if mode == "ai":
                if auto_redfox:
                    redfox_api_key = (data.get("redfox_api_key") or "").strip() or RUNTIME["redfox_api_key"]
                    try:
                        extra = gather_generation_inspiration(topic, redfox_api_key=redfox_api_key)
                        if extra:
                            real_notes = list(real_notes) + extra
                    except Exception:
                        pass
                res = generate_with_ai(topic, hook, voice, insight, cut, real_notes, api_key, base_url, model, brief, strategy, theme=theme)
            else:
                res = generate_long_image_markdown(topic, angle, hook, voice, insight, cut, theme=theme)
                ttags, ttags_list = normalize_xhs_tags(res.get("tags", ""))
                body_lines = []
                for p in res.get("pages", []):
                    if p.get("type") != "page":
                        continue
                    if p.get("title"):
                        body_lines.append(f"## {p['title']}")
                        body_lines.append("")
                    body_lines.append(p.get("body", "").strip())
                    body_lines.append("")
                res["cover_title"] = res.get("title", topic)
                res["detail_title"] = res.get("title", topic)
                res["core_viewpoint"] = res.get("core_viewpoint", "")
                res["tags"] = ttags
                res["tags_list"] = ttags_list
                res["body"] = "\n".join(body_lines).strip()
                _title = res.get("cover_title") or res.get("title") or ""
                res["title_length"] = len(_title)
                res["title_too_long"] = len(_title) > TITLE_MAX_LEN

            res["ok"] = True
            res["idx"] = idx
            res["topic"] = topic
            res["theme"] = theme
            results.append(res)

            # 无论是否标记已用，都把成品回写选题条目，方便在选题库直接查看/复制成品
            item_id = it.get("id")
            for bi in bank_items:
                if (item_id and str(bi.get("id", "")) == str(item_id)) or \
                   _normalize_for_dedupe(bi.get("topic", "")) == _normalize_for_dedupe(topic):
                    bi["generated"] = {
                        "mode": mode,
                        "cover_title": res.get("cover_title") or res.get("title") or topic,
                        "detail_title": res.get("detail_title") or res.get("title") or topic,
                        "core_viewpoint": res.get("core_viewpoint", ""),
                        "body": res.get("body", ""),
                        "tags": res.get("tags", ""),
                        "tags_list": res.get("tags_list", []),
                        "theme": theme,
                        "voice": voice,
                        "insight": insight,
                        "generated_at": now,
                    }
                    dirty = True
                    if mark_used:
                        if bi.get("status") != "used":
                            bi["status"] = "used"
                            bi["used_at"] = now
                            generated_ids.add(str(bi.get("id", "")))
                    break
        except Exception as e:
            results.append({"ok": False, "idx": idx, "topic": topic, "error": str(e)})

    if dirty:
        save_idea_bank(bank_items)

    counts = {"all": len(bank_items), "draft": 0, "used": 0, "archived": 0}
    for bi in bank_items:
        st = bi.get("status", "draft")
        if st in counts:
            counts[st] += 1

    return jsonify({
        "ok": True,
        "mode": mode,
        "total": len(items),
        "success": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "results": results,
        "counts": counts,
    })


@app.route("/api/match-voice-insight", methods=["POST"])
def api_match_voice_insight():
    """根据 topic / scene / theme 自动匹配语气和独到见解角度。"""
    data = request.get_json() or {}
    topic = (data.get("topic") or "").strip()
    scene = (data.get("scene") or "").strip()
    theme = (data.get("theme") or data.get("naval_theme") or "").strip()
    hook = (data.get("hook") or "").strip()

    if not topic and not scene:
        return jsonify({"error": "topic 或 scene 至少提供一个"}), 400

    # 把 topic、scene、hook 拼起来做场景关键词匹配
    combined = f"{topic} {scene} {hook}".strip()
    voice = _voice_for_scene(combined)
    insight = _insight_for_scene(combined)

    # 如果 scene/hook 没有给出强信号，用 theme 兜底，保证纳瓦尔方向有明确语气
    theme_voice_fallback = {
        "财富": "算账拆解型",
        "复利": "算账拆解型",
        "时间": "算账拆解型",
        "金钱": "算账拆解型",
        "注意力": "反骨警示型",
        "消费": "反骨警示型",
        "自由": "温柔坚定型",
        "幸福": "温柔坚定型",
        "健康": "温柔坚定型",
        "判断力": "观点刺穿型",
        "决策": "观点刺穿型",
        "杠杆": "观点刺穿型",
        "专长": "故事共鸣型",
        "产品化": "故事共鸣型",
        "学习": "故事共鸣型",
    }
    theme_insight_fallback = {
        "财富": "算账视角",
        "复利": "算账视角",
        "时间": "算账视角",
        "金钱": "算账视角",
        "注意力": "身份重构",
        "消费": "身份重构",
        "自由": "身份重构",
        "幸福": "反常识",
        "健康": "反常识",
        "判断力": "反常识",
        "决策": "二元对立",
        "杠杆": "二元对立",
        "专长": "亲身经历",
        "产品化": "亲身经历",
        "学习": "亲身经历",
    }
    if voice == "清醒陪伴型" and theme in theme_voice_fallback:
        voice = theme_voice_fallback[theme]
    if insight == "反常识" and theme in theme_insight_fallback:
        insight = theme_insight_fallback[theme]

    vt = VOICE_TEMPLATES.get(voice, {})
    ia = INSIGHT_ANGLES.get(insight, "")
    return jsonify({
        "ok": True,
        "recommended_voice": voice,
        "recommended_insight": insight,
        "voice_hint": vt.get("句式特点", ""),
        "insight_hint": ia,
        "reason": f"基于「{topic or scene}」的场景关键词匹配：语气采用“{voice}”，见解采用“{insight}”。",
    })


@app.route("/api/first-comment", methods=["POST"])
def api_first_comment():
    """为文章生成一条能引发深度讨论的置顶首评。
    要求：紧扣议题、提出启发观点/合理质疑/延伸视角、引导读者突破表层反思、语气贴合文章风格、绝不复述原文。
    文章标题限制：不超过 20 个字符（含标点，小红书硬性上限）；超限直接拒绝。"""
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()
    topic = (data.get("topic") or title or "").strip()
    voice = (data.get("voice") or "").strip()
    strategy = (data.get("strategy") or "").strip()
    if not title and not body:
        return jsonify({"error": "请提供文章标题或正文"}), 400

    # 文章标题字数硬限制：不超过 TITLE_MAX_LEN 个字符
    if title and len(title) > TITLE_MAX_LEN:
        return jsonify({
            "error": f"文章标题需不超过 {TITLE_MAX_LEN} 个字符（当前 {len(title)} 字），请精简标题后再生成首评。",
            "title_length": len(title),
            "title_max_len": TITLE_MAX_LEN,
        }), 400

    api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
    base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
    model = (data.get("model") or "").strip() or RUNTIME["default_model"]

    if not api_key or not base_url:
        # 无大模型 Key：回退本地模板首评（仍可用）
        comment = build_local_first_comment(title, body, topic, voice, strategy)
        return jsonify({
            "comment": comment,
            "discussion_hook": "",
            "source": "local-template",
            "title_length": len(title),
            "warn": "未配置大模型 Key，已用本地模板生成首评（仍可引发讨论）。",
        })

    try:
        comment, hook = generate_first_comment(
            title, body, topic, voice, strategy, api_key, base_url, model
        )
        return jsonify({
            "comment": comment,
            "discussion_hook": hook,
            "source": "ai",
            "title_length": len(title),
        })
    except Exception as e:
        # AI 失败：回退本地模板，保证可用
        comment = build_local_first_comment(title, body, topic, voice, strategy)
        return jsonify({
            "comment": comment,
            "source": "local-template",
            "title_length": len(title),
            "warn": f"AI 首评失败，已用本地模板：{e}",
        })


@app.route("/api/refresh-cut", methods=["POST"])
def api_refresh_cut():
    """为指定主题/场景/爆款标题生成若干条不重复的真实切口。
    优先用 AI 生成（若 key 可用），否则从本地大库随机抽取。
    接收 Step 1 全量上下文（voice/insight/hook/core_quote），确保切口与上方逻辑严格匹配。"""
    data = request.get_json() or {}
    topic = (data.get("topic") or data.get("theme") or "").strip()
    scene = (data.get("scene") or "").strip()
    hot_title = (data.get("hot_title") or "").strip()
    count = max(1, min(10, int(data.get("count", 3))))
    used_cuts = data.get("used_cuts") or []

    api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
    base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
    model = (data.get("model") or "").strip() or RUNTIME["default_model"]

    voice = (data.get("voice") or "").strip()
    insight = (data.get("insight") or "").strip()
    hook = (data.get("hook") or "").strip()
    core_quote = (data.get("core_quote") or "").strip()

    # 把原始 topic 传给 AI，让 AI 基于真实选题生成；本地 fallback 模板池再在内部做归一化
    cuts = generate_fresh_cuts(
        theme=topic,
        scene=scene or topic,
        hot_title=hot_title,
        count=count,
        api_key=api_key,
        base_url=base_url,
        model=model,
        _used=used_cuts,
        voice=voice,
        insight=insight,
        hook=hook,
        core_quote=core_quote,
        topic=topic,
    )
    return jsonify({
        "theme": topic,
        "scene": scene,
        "count": len(cuts),
        "cuts": cuts,
        "source": "ai" if (api_key and base_url and model) else "local",
    })


@app.route("/api/core-quotes", methods=["POST"])
def api_core_quotes():
    """根据用户选题，从纳瓦尔真实思想库中匹配最相关主题，生成核心金句（文章钩子）。
    金句必须根植于纳瓦尔思想，不能围绕用户的具体场景（销售/播客/上班）瞎编。"""
    data = request.get_json() or {}
    topic = (data.get("topic") or data.get("theme") or "").strip()
    if not topic:
        return jsonify({"error": "请先输入选题关键词"}), 400

    count = max(1, min(12, int(data.get("count", 6))))
    used_quotes = data.get("used_quotes") or []

    api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
    base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
    model = (data.get("model") or "").strip() or RUNTIME["default_model"]

    used = set(used_quotes)
    quotes = generate_core_quotes(
        topic=topic,
        count=count,
        api_key=api_key,
        base_url=base_url,
        model=model,
        used=used,
    )
    matched_themes = _match_topic_to_naval_themes(topic)
    return jsonify({
        "topic": topic,
        "matched_naval_themes": matched_themes,
        "count": len(quotes),
        "quotes": quotes,
        "source": "ai" if (api_key and base_url and model) else "local",
    })


@app.route("/api/shorten-title", methods=["POST"])
def api_shorten_title():
    """把超长标题压缩到小红书硬性上限（≤20字），保留核心钩子与关键词。
    优先用 AI；无 Key 时用规则兜底（截取前 20 字并智能断句）。"""
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    topic = (data.get("topic") or "").strip()
    if not title:
        return jsonify({"error": "请提供需要精简的标题"}), 400

    api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
    base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
    model = (data.get("model") or "").strip() or RUNTIME["default_model"]

    original_len = len(title)
    if original_len <= TITLE_MAX_LEN:
        return jsonify({
            "title": title,
            "original_length": original_len,
            "title_length": original_len,
            "title_too_long": False,
            "source": "no-change",
        })

    # AI 精简
    if api_key and base_url and model:
        try:
            system_prompt = f"""你是一位小红书标题专家。请把用户给出的标题压缩到 {TITLE_MAX_LEN} 个字符以内（含标点），必须保留最核心的钩子、关键词和情绪。
要求：
1. 只返回精简后的标题，不要解释、不要 markdown。
2. 标题要有小红书风格：反差、利益清晰、带钩子。
3. 字数严格≤{TITLE_MAX_LEN}，超出会失败。"""
            user_prompt = f"原标题：{title}\n主题：{topic or '无'}\n请输出精简后标题："
            shortened = call_openai_compatible(system_prompt, user_prompt, api_key, base_url, model, timeout=20).strip()
            # 清理可能的引号/前缀
            shortened = re.sub(r'^[\"\'\s]+|[\"\'\s]+$', '', shortened)
            if shortened and len(shortened) <= TITLE_MAX_LEN:
                return jsonify({
                    "title": shortened,
                    "original_length": original_len,
                    "title_length": len(shortened),
                    "title_too_long": False,
                    "source": "ai",
                })
        except Exception:
            pass

    # 规则兜底：在 TITLE_MAX_LEN 内找最后一个标点/语气词断句，避免硬生生截断
    shortened = title[:TITLE_MAX_LEN]
    # 从末尾向前找可断句位置（标点、连词、语气词）
    for i in range(TITLE_MAX_LEN - 1, max(TITLE_MAX_LEN // 2, 0), -1):
        if title[i] in "，。、；：？！,.;:!?":
            shortened = title[:i]
            break
    # 若截断后失去核心含义，直接保留前 20 字
    if not shortened.strip():
        shortened = title[:TITLE_MAX_LEN]
    return jsonify({
        "title": shortened,
        "original_length": original_len,
        "title_length": len(shortened),
        "title_too_long": len(shortened) > TITLE_MAX_LEN,
        "source": "rule",
    })


@app.route("/api/qc", methods=["POST"])
def api_qc():
    """质检AI：帖子包（markdown + 五要素字段） -> 质检报告 JSON"""
    data = request.get_json() or {}
    markdown = data.get("markdown", "").strip()
    topic = data.get("topic", "").strip()
    if not markdown:
        return jsonify({"error": "无内容可质检"}), 400
    api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
    base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
    model = (data.get("model") or "").strip() or RUNTIME["default_model"]
    if not api_key:
        return jsonify({"error": "质检需要大模型 API Key。请先在上方 API 配置里填写。"}), 400

    result = qc_markdown(
        markdown, topic, api_key, base_url, model,
        cover_title=data.get("cover_title", ""),
        detail_title=data.get("detail_title", ""),
        core_viewpoint=data.get("core_viewpoint", ""),
        tags_str=data.get("tags", ""),
    )
    return jsonify(result)


@app.route("/api/rewrite", methods=["POST"])
def api_rewrite():
    """改写AI：根据 QC 报告建议，对当前帖子进行改写 -> 新帖子包 JSON"""
    data = request.get_json() or {}
    markdown = data.get("markdown", "").strip()
    qc_report = data.get("qc_report") or {}
    topic = data.get("topic", "").strip()
    if not markdown:
        return jsonify({"error": "无内容可改写"}), 400
    if not qc_report:
        return jsonify({"error": "请先完成质检（/api/qc）"}), 400
    api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
    base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
    model = (data.get("model") or "").strip() or RUNTIME["default_model"]
    if not api_key:
        return jsonify({"error": "改写需要大模型 API Key。请先在上方 API 配置里填写。"}), 400

    result = rewrite_with_ai(
        topic=topic or data.get("cover_title", ""),
        hook=data.get("hook", ""),
        voice=data.get("voice", "清醒陪伴型"),
        insight=data.get("insight", "反常识"),
        cut=data.get("cut", ""),
        markdown=markdown,
        qc_report=qc_report,
        api_key=api_key,
        base_url=base_url,
        model=model,
        theme=data.get("theme") or data.get("naval_theme") or "",
    )
    return jsonify(result)


@app.route("/api/title-score", methods=["POST"])
def api_title_score():
    """title-score 红狐技能：真实爆款数据 + LLM 六维加权评分。"""
    data = request.get_json() or {}
    title = data.get("title", "").strip()
    topic = data.get("topic", "").strip() or title
    if not title:
        return jsonify({"error": "请传入待评分标题 title"}), 400
    api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
    base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
    model = (data.get("model") or "").strip() or RUNTIME["default_model"]
    redfox_api_key = (data.get("redfox_api_key") or "").strip() or RUNTIME["redfox_api_key"]
    if not api_key:
        return jsonify({"error": "标题评分需要大模型 API Key。请先在上方 API 配置里填写。"}), 400
    result = score_title_with_redfox(title, topic, api_key, base_url, model, redfox_api_key=redfox_api_key)
    return jsonify(result)


@app.route("/api/note-analyze", methods=["POST"])
def api_note_analyze():
    """note-analyzer 红狐技能：真实爆款数据 + LLM 四维度评分对标。"""
    data = request.get_json() or {}
    body = data.get("body", "").strip()
    topic = data.get("topic", "").strip()
    if not body:
        return jsonify({"error": "请传入笔记正文 body"}), 400
    api_key = (data.get("api_key") or "").strip() or RUNTIME["openai_key"]
    base_url = (data.get("base_url") or "").strip() or RUNTIME["openai_base"]
    model = (data.get("model") or "").strip() or RUNTIME["default_model"]
    redfox_api_key = (data.get("redfox_api_key") or "").strip() or RUNTIME["redfox_api_key"]
    if not api_key:
        return jsonify({"error": "笔记体检需要大模型 API Key。请先在上方 API 配置里填写。"}), 400
    result = analyze_note_with_redfox(body, topic, api_key, base_url, model, redfox_api_key=redfox_api_key)
    return jsonify(result)


@app.route("/api/export-import", methods=["POST"])
def api_export_import():
    """把当前生成的五要素帖子转成『小红书纳瓦尔图文生成器.html』可一键导入的纯文本格式。

    接收前端传来的 post 对象（cover_title / body / core_viewpoint / tags / word_count / read_time 等），
    返回 {"text": 导入文本}。前端可复制或下载为 .md 后粘贴/导入到纳瓦尔图文生成器。"""
    data = request.get_json() or {}
    text = build_import_text(data)
    return jsonify({"text": text})


def _get_lan_ip():
    """返回本机局域网 IP（用于同一 Wi-Fi 下手机/其他电脑访问）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


@app.route("/api/network", methods=["GET"])
def api_network():
    """返回局域网访问信息，供前端生成二维码与复制链接（多设备访问用）。"""
    port = int(os.environ.get("PORT", "8765"))
    lan = _get_lan_ip()
    return jsonify({
        "lan_ip": lan,
        "port": port,
        "lan_url": f"http://{lan}:{port}",
        "localhost_url": f"http://127.0.0.1:{port}",
        "host": os.environ.get("HOST", "0.0.0.0"),
    })


@app.route("/api/qr", methods=["GET"])
def api_qr():
    """生成二维码 PNG（默认指向局域网访问地址），供手机扫码打开。"""
    port = int(os.environ.get("PORT", "8765"))
    text = (request.args.get("text", "") or "").strip() or f"http://{_get_lan_ip()}:{port}"
    try:
        import qrcode
        from io import BytesIO
        img = qrcode.make(text)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return Response(buf.getvalue(), mimetype="image/png")
    except ImportError:
        return jsonify({"error": "未安装 qrcode 库，请执行 pip install qrcode"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tunnel", methods=["GET"])
def api_tunnel():
    """返回 Cloudflare 隧道当前公网地址（如有），供前端多设备面板展示。"""
    try:
        tp = os.path.join(BASE_DIR, "tunnel_url.txt")
        if os.path.exists(tp):
            with open(tp, "r", encoding="utf-8") as f:
                url = f.read().strip()
            if url:
                return jsonify({"tunnel_url": url, "ready": True})
        return jsonify({"tunnel_url": "", "ready": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    import logging
    host = os.environ.get("HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("PORT", "8765"))
    except ValueError:
        port = 8765
    # 后台无窗口运行时，把日志写入 flask.log 便于排错
    try:
        logging.basicConfig(
            filename=os.path.join(BASE_DIR, "flask.log"),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
        )
        logging.info("Flask 启动 host=%s port=%s", host, port)
    except Exception:
        pass
    app.run(host=host, port=port, debug=False)
