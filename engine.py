"""SNOW WATCH 서버 (로컬 전용)

크리스마스 마켓 라이브캠 관제실의 뒤쪽 일을 맡는다.
  - 유튜브 라이브 여부를 API 키 없이, 천천히(한 번에 하나씩) 확인한다
  - 현지 날씨 예보(Open-Meteo)와 가까운 공항의 실제 관측(METAR)을 받아 온다
  - 라이브 썸네일을 5분마다 받아 두고, 화면 분석 결과와 합쳐 '눈 대조' 판정을 낸다
  - 검사 기록과 판정은 data/ 폴더에 남긴다

실행: python server.py  →  http://localhost:8765
"""
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8765))
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
THUMB_DIR = os.path.join(DATA, "thumbs")
CAMS_FILE = os.path.join(ROOT, "cams.json")
LOG_FILE = os.path.join(DATA, "check-log.jsonl")
SNOW_LOG_FILE = os.path.join(DATA, "snow-log.jsonl")
STATE_FILE = os.path.join(DATA, "state.json")
VERDICT_FILE = os.path.join(DATA, "verdicts.json")
PIN_FILE = os.path.join(DATA, "pins.json")
STATION_FILE = os.path.join(DATA, "stations.json")
os.makedirs(THUMB_DIR, exist_ok=True)

# ---- 주기 설정 -------------------------------------------------------------
YT_RECHECK_MIN = 30    # 유튜브 라이브 여부는 캠마다 30분에 한 번만 다시 묻는다
YT_RETRY_MIN = 10      # 확인에 실패한 캠은 10분 뒤에 다시 시도한다
YT_GAP_SEC = 8         # 유튜브 요청 사이 간격
BACKOFF_MIN = 60       # 유튜브가 막으면(429) 이만큼 쉰다
WEATHER_MIN = 10       # 예보·공항 관측 갱신 주기
THUMB_MIN = 5          # 유튜브 라이브 썸네일은 5분마다 바뀐다
SNAP_MIN = 15          # 사진형 웹캠 확인 주기
SNAP_FRESH_MIN = 180   # 이보다 오래된 사진이면 '갱신 멈춤'
WATCH_IDLE_MIN = 3     # 화면을 보는 사람이 없으면 썸네일 갱신을 쉰다
STATIC_AFTER = 6       # 썸네일이 이 횟수(약 30분) 동안 그대로면 '고정 이미지'로 본다
THUMB_LIVE_MIN = 12    # 이 시간 안에 썸네일이 바뀐 캠만 '지금 화면'으로 믿는다
OBS_MAX_KM = 60        # 이보다 먼 공항 관측은 참고만 한다
OBS_MAX_AGE_MIN = 90   # 이보다 오래된 관측은 판정에 쓰지 않는다
EYE_VALID_MIN = 60     # 사람이 누른 '눈 보여요/안 보여요'의 유효 시간
VISION_VALID_MIN = 20  # 화면 분석 결과의 유효 시간

# 화면 분석(실험적) 기준값. 눈발은 캠마다 '눈 없는 평소 값'을 배운 뒤에만 판정한다.
# 2026-11-11 실측: 눈 없는 밤 화면의 새 점 비율은 뮌헨 0.21, 로바니에미 0.13~0.21, 비 오는 아우데나르더 0.26,
# 물결·구름이 많은 트롬쇠 0.47, 온천 김이 나는 구사쓰 0.66. 합성 눈은 0.8 이상.
FALL_MIN_CELLS = 0.40      # 화면 칸의 40% 이상에서 새 점이 보여야 눈발 후보
FALL_LEARN_N = 3           # 평소 값을 배우는 데 필요한 '눈 없는 때'의 비교 횟수
FALL_MAX_THR = 0.85        # 기준이 이보다 높아지는 캠은 화면으로 눈발을 가릴 수 없다
COVER_UNCAL_WHITE = 0.45   # 평소 값을 모를 때: 화면 아래쪽의 45% 이상이 흰색이면 쌓인 눈 후보
COVER_OVER_BASE = 0.15     # 평소보다 흰 면적이 이만큼 늘면 쌓인 눈 후보

SNOW_CODES = {71, 73, 75, 77, 85, 86}

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# 제목에 이런 말이 있으면 '실시간 광장 캠'이 아니라 편집·재방송일 가능성이 높다
SUSPECT_WORDS = [
    "walk", "tour", "lofi", "lo-fi", "music", "relax", "chill", "rewind", "rolling",
    "journey", "random", "highlight", "compilation", "ambience", "ambient", "timelapse",
    "time-lapse", "beats", "asmr", "drone",
]

L = threading.RLock()
S = {            # 서버를 다시 켜도 이어 쓰는 상태 (data/state.json)
    "checks": {},      # camId -> 라이브 확인 결과
    "weather": {},     # camId -> 예보 모델의 현재 날씨
    "obs": {},         # camId -> 가까운 공항의 실제 관측
    "thumbs": {},      # camId -> 썸네일 정보 (바이트는 thumb_bytes / data/thumbs)
    "vision": {},      # camId -> 화면 분석 수치 (브라우저가 계산해서 보냄)
    "baseline": {},    # camId -> 눈 없는 평소의 화면 수치
    "labels": {},      # camId -> 사람이 누른 눈 확인
    "snow": {},        # camId -> 눈 대조 판정
    "events": [],      # 최근 눈 판정 변화
    "blockedUntil": 0.0,
}
thumb_bytes = {}       # camId -> 최신 썸네일 바이트
stats = {}             # camId -> 누적 검사 통계
last_try = {}          # camId -> 마지막으로 유튜브에 물어본 시각
forced = []            # 바로 다시 확인할 캠 (사용자가 누른 것)
last_poll = 0.0        # 화면이 마지막으로 상태를 받아 간 시각
_cams_cache = {"mtime": None, "cams": []}
_state_saved = 0.0


class RateLimited(Exception):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def minutes_since(iso):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 60
    except (TypeError, ValueError):
        return 1e9


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with L:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def append_line(path, obj):
    with L:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_cams():
    """cams.json은 바뀌었을 때만 다시 읽는다."""
    try:
        mtime = os.path.getmtime(CAMS_FILE)
    except OSError:
        return []
    with L:
        if _cams_cache["mtime"] != mtime:
            _cams_cache["cams"] = load_json(CAMS_FILE, [])
            _cams_cache["mtime"] = mtime
        return _cams_cache["cams"]


def save_state(force=False):
    global _state_saved
    if not force and time.time() - _state_saved < 30:
        return
    _state_saved = time.time()
    with L:
        save_json(STATE_FILE, S)


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read(), r.headers


def fetch(url):
    final, body, _ = http_get(url)
    if "google.com/sorry" in final:  # 유튜브가 요청을 막고 확인 페이지로 보냄
        raise RateLimited()
    return final, body.decode("utf-8", "replace")


# ---- 유튜브 라이브 확인 ------------------------------------------------------
def extract_json(html, var):
    m = re.search(r"var " + var + r" = (\{.*?\});(?:var |</script>)", html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def inspect_video(video_id):
    """유튜브 시청 페이지를 읽어 라이브 상태와 근거를 돌려준다."""
    _, html = fetch("https://www.youtube.com/watch?v=" + video_id)
    pr = extract_json(html, "ytInitialPlayerResponse") or {}
    vd = pr.get("videoDetails", {})
    ps = pr.get("playabilityStatus", {})
    if "not a bot" in (ps.get("reason") or ""):   # 유튜브의 봇 확인 화면: 방송 상태를 알 수 없다
        raise RateLimited()
    lbd = (pr.get("microformat", {}).get("playerMicroformatRenderer", {})
           .get("liveBroadcastDetails", {}))
    watching = None
    m = re.search(r'"text":"([\d,\.]+[KkMm]?)"\},\{"text":" watching', html) or \
        re.search(r'([\d,]+) watching now', html)
    if m:
        watching = m.group(1)

    if not vd:
        status = "unavailable"
    elif lbd.get("isLiveNow"):
        status = "live"
    elif vd.get("isUpcoming"):
        status = "upcoming"
    elif lbd.get("endTimestamp") or (vd.get("isLiveContent") and not lbd.get("isLiveNow")):
        status = "ended"
    elif not vd.get("isLiveContent"):
        status = "not_live"
    else:
        status = "unknown"
    if ps.get("status") not in (None, "OK") and status != "live":
        status = "unavailable"

    title = vd.get("title", "")
    return {
        "videoId": video_id,
        "status": status,
        "title": title,
        "author": vd.get("author"),
        "channelId": vd.get("channelId"),
        "startTimestamp": lbd.get("startTimestamp"),
        "endTimestamp": lbd.get("endTimestamp"),
        "watching": watching,
        "playableInEmbed": ps.get("playableInEmbed"),
        "reason": ps.get("reason"),
        "suspectWords": [w for w in SUSPECT_WORDS if w in title.lower()],
    }


def channel_live_video(channel_id):
    """채널이 지금 내보내는 라이브 영상 ID (없으면 None)."""
    _, html = fetch("https://www.youtube.com/channel/" + channel_id + "/live")
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', html)
    return m.group(1) if m else None


def check_youtube(cam):
    info = inspect_video(cam["videoId"])
    # 등록된 영상이 끝났으면 같은 채널의 현재 라이브를 찾아본다
    if info["status"] != "live" and cam.get("channelId"):
        alt = channel_live_video(cam["channelId"])
        if alt and alt != cam["videoId"]:
            alt_info = inspect_video(alt)
            if alt_info["status"] == "live":
                alt_info["replacedFrom"] = cam["videoId"]
                return alt_info
    return info


def set_blocked():
    with L:
        S["blockedUntil"] = time.time() + BACKOFF_MIN * 60
    save_state(force=True)


def add_stat(cam_id, status, at):
    s = stats.setdefault(cam_id, {"checks": 0, "live": 0, "lastLive": None})
    s["checks"] += 1
    if status in ("live", "fresh"):
        s["live"] += 1
        s["lastLive"] = at


def load_stats():
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("status") != "error" and not r.get("fallback"):
                    add_stat(r["camId"], r.get("status"), r.get("checkedAt"))
                    # 예전 버전의 기록만 있고 state.json이 없을 때를 위한 복구
                    if r["camId"] not in S["checks"] and "title" in r:
                        S["checks"][r["camId"]] = r
    except OSError:
        pass


def run_youtube_check(cam):
    cid = cam["id"]
    last_try[cid] = time.time()
    try:
        res = check_youtube(cam)
    except RateLimited:
        set_blocked()
        return note_error(cid, "유튜브 요청 제한(429)")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            set_blocked()
            return note_error(cid, "유튜브 요청 제한(429)")
        return note_error(cid, str(e)[:160])
    except Exception as e:  # 네트워크 오류 등
        return note_error(cid, str(e)[:160])
    at = now_iso()
    with L:
        prev = S["checks"].get(cid, {})
        if prev.get("videoId") != res["videoId"]:
            S["thumbs"].pop(cid, None)  # 영상이 바뀌면 썸네일도 새로 받는다
        S["checks"][cid] = {**res, "camId": cid, "checkedAt": at}
        add_stat(cid, res["status"], at)
    append_line(LOG_FILE, {"camId": cid, "checkedAt": at, "status": res["status"],
                           "videoId": res["videoId"], "watching": res["watching"]})
    save_state()


def note_error(cid, msg):
    """확인 실패: 지난 결과는 그대로 두고 실패 사실만 적는다."""
    with L:
        c = S["checks"].setdefault(cid, {"camId": cid, "status": "error"})
        c["lastError"] = msg
        c["lastErrorAt"] = now_iso()


def next_due_cam():
    if time.time() < S["blockedUntil"]:
        return None
    cams = [c for c in load_cams() if c.get("type", "youtube") == "youtube" and c.get("videoId")]
    by_id = {c["id"]: c for c in cams}
    with L:
        while forced:
            cid = forced.pop(0)
            if cid in by_id:
                return by_id[cid]
    due = []
    for c in cams:
        chk = S["checks"].get(c["id"], {})
        age = minutes_since(chk.get("checkedAt")) if chk.get("checkedAt") else 1e9
        tried = (time.time() - last_try.get(c["id"], 0)) / 60
        if age >= YT_RECHECK_MIN and tried >= YT_RETRY_MIN:
            due.append((-age, c["id"], c))
    return min(due)[2] if due else None


def yt_worker():
    while True:
        try:
            cam = next_due_cam()
            if cam:
                run_youtube_check(cam)
                time.sleep(YT_GAP_SEC)
            else:
                time.sleep(3)
        except Exception as e:
            print("유튜브 확인 오류:", e)
            time.sleep(15)


# ---- 날씨: 예보 모델 + 공항 관측 ---------------------------------------------
def update_weather():
    """예보를 20곳씩 받는다. 한 묶음이 3번 실패하면 그 이유를 S["weatherNote"]에 적고, 마지막에 예외로 알린다."""
    cams = [c for c in load_cams() if c.get("lat") is not None and c.get("lon") is not None]
    errors = []
    for i in range(0, len(cams), 20):
        chunk = cams[i:i + 20]
        for attempt in range(3):
            try:
                _update_weather_chunk(chunk)
                break
            except Exception as e:
                if attempt == 2:
                    errors.append(f"{type(e).__name__}: {e}"[:120])
                else:
                    time.sleep(3 * (attempt + 1))
        time.sleep(1)
    with L:
        S["weatherNote"] = {"at": now_iso(), "errors": errors}
    if errors:
        raise RuntimeError("; ".join(errors)[:300])


def _update_weather_chunk(cams):
    if not cams:
        return
    url = ("https://api.open-meteo.com/v1/forecast?latitude=" + ",".join(str(c["lat"]) for c in cams)
           + "&longitude=" + ",".join(str(c["lon"]) for c in cams)
           + "&current=temperature_2m,weather_code,is_day,snowfall,snow_depth,precipitation,rain"
           + "&timezone=auto")
    _, body, _ = http_get(url, timeout=20)
    data = json.loads(body)
    if isinstance(data, dict):
        data = [data]
    at = now_iso()
    with L:
        for c, w in zip(cams, data):
            cur = w.get("current")
            if not cur:
                continue
            S["weather"][c["id"]] = {
                "t": cur.get("temperature_2m"), "code": cur.get("weather_code"),
                "isDay": cur.get("is_day"), "snowfall": cur.get("snowfall") or 0,
                "depth": cur.get("snow_depth") or 0, "precip": cur.get("precipitation") or 0,
                "rain": cur.get("rain") or 0, "utcOffset": w.get("utc_offset_seconds", 0),
                "fetchedAt": at,
            }


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(a))


def find_station(lat, lon):
    """캠에서 가장 가까운, 관측을 내는 공항을 찾는다."""
    d = 0.9
    dl = d / max(0.2, math.cos(math.radians(lat)))
    url = ("https://aviationweather.gov/api/data/metar?format=json&bbox="
           f"{lat - d:.2f},{lon - dl:.2f},{lat + d:.2f},{lon + dl:.2f}")
    _, body, _ = http_get(url, timeout=25)
    try:
        rows = json.loads(body or b"[]")
    except ValueError:
        rows = []
    best = None
    for m in rows:
        if m.get("lat") is None or not m.get("icaoId"):
            continue
        km = haversine_km(lat, lon, m["lat"], m["lon"])
        if best is None or km < best["km"]:
            best = {"icao": m["icaoId"], "name": (m.get("name") or "").split(",")[0], "km": round(km)}
    return best or {"icao": None}


def update_stations():
    stations = load_json(STATION_FILE, {})
    changed = False
    for c in load_cams():
        if c.get("lat") is None or c.get("lon") is None:
            continue
        key = f'{c["lat"]:.3f},{c["lon"]:.3f}'
        st = stations.get(key)
        # 못 찾은 곳은 하루 뒤에 다시 찾아본다 (밤에는 관측을 쉬는 공항이 있다)
        if st and (st.get("icao") or minutes_since(st.get("at")) < 1440):
            continue
        try:
            st = find_station(c["lat"], c["lon"])
        except Exception:
            continue
        st["at"] = now_iso()
        stations[key] = st
        changed = True
        time.sleep(0.4)
    if changed:
        save_json(STATION_FILE, stations)
    return stations


WX_WORDS = [
    ("FZRA", "어는 비"), ("FZDZ", "어는 이슬비"), ("RASN", "진눈깨비"), ("SNRA", "진눈깨비"),
    ("SHSN", "소낙눈"), ("SN", "눈"), ("SG", "싸락눈"), ("GS", "싸락우박"), ("PL", "얼음싸라기"),
    ("SHRA", "소나기"), ("RA", "비"), ("DZ", "이슬비"), ("FG", "안개"), ("BR", "박무"), ("HZ", "연무"),
]


def parse_wx(wx):
    """METAR 현재일기 문자열을 눈/비 여부와 한국어 설명으로 바꾼다."""
    tokens = (wx or "").split()
    snow = rain = False
    labels = []
    for t in tokens:
        drifting = "DRSN" in t or "BLSN" in t  # 땅에 쌓인 눈이 날리는 것은 '내리는 눈'이 아니다
        if not drifting and re.search(r"SN|SG|GS|PL", t):
            snow = True
        if re.search(r"RA|DZ", t):
            rain = True
        word = "날린 눈" if drifting else next((k for code, k in WX_WORDS if code in t), None)
        if word:
            labels.append(("약한 " if t.startswith("-") else "강한 " if t.startswith("+") else "") + word)
    return snow, rain, ", ".join(dict.fromkeys(labels)) or ("특이 현상 없음" if not tokens else wx)


def update_obs():
    stations = update_stations()
    cams = [c for c in load_cams() if c.get("lat") is not None and c.get("lon") is not None]
    cam_st = {c["id"]: stations.get(f'{c["lat"]:.3f},{c["lon"]:.3f}') for c in cams}
    ids = sorted({st["icao"] for st in cam_st.values() if st and st.get("icao")})
    if not ids:
        return
    url = "https://aviationweather.gov/api/data/metar?format=json&ids=" + ",".join(ids)
    _, body, _ = http_get(url, timeout=30)
    try:
        rows = json.loads(body or b"[]")
    except ValueError:
        rows = []
    latest = {}
    for m in rows:
        prev = latest.get(m.get("icaoId"))
        if not prev or (m.get("obsTime") or 0) > (prev.get("obsTime") or 0):
            latest[m.get("icaoId")] = m
    with L:
        for cid, st in cam_st.items():
            m = latest.get(st.get("icao")) if st else None
            if not m:
                S["obs"].pop(cid, None)
                continue
            snow, rain, label = parse_wx(m.get("wxString"))
            S["obs"][cid] = {
                "icao": st["icao"], "name": st.get("name"), "km": st.get("km"),
                "time": datetime.fromtimestamp(m.get("obsTime") or 0, timezone.utc).isoformat(timespec="seconds"),
                "wx": m.get("wxString") or "", "label": label, "snow": snow, "rain": rain,
                "t": m.get("temp"), "raw": m.get("rawOb"),
            }


# ---- 썸네일 ------------------------------------------------------------------
def thumb_path(cid):
    return os.path.join(THUMB_DIR, re.sub(r"[^\w-]", "_", cid) + ".jpg")


def update_thumb(cam):
    cid = cam["id"]
    kind = cam.get("type", "youtube")
    with L:
        meta = dict(S["thumbs"].get(cid, {}))
        vid = S["checks"].get(cid, {}).get("videoId") or cam.get("videoId")
    if kind == "snapshot":
        urls = [cam["imageUrl"]]
    elif kind == "youtube" and vid:
        urls = [f"https://i.ytimg.com/vi/{vid}/maxresdefault_live.jpg",
                f"https://i.ytimg.com/vi/{vid}/hqdefault_live.jpg"]
        if meta.get("url") in urls:  # 지난번에 통한 주소부터 묻는다
            urls.sort(key=lambda u: u != meta["url"])
    else:
        return
    body = headers = used = None
    unchanged = False
    for url in urls:
        cond = {"If-None-Match": meta["etag"]} if meta.get("etag") and meta.get("url") == url else {}
        try:
            _, body, headers = http_get(url, cond, timeout=15)
            used = url
            break
        except urllib.error.HTTPError as e:
            if e.code == 304:
                unchanged, used = True, url
                break
        except Exception:
            pass
    at = now_iso()
    meta["fetchedAt"] = at
    if used is None:
        meta["error"] = True
    else:
        meta.pop("error", None)
        digest = meta.get("hash") if unchanged else hashlib.md5(body).hexdigest()[:12]
        if unchanged or digest == meta.get("hash"):
            meta["same"] = meta.get("same", 0) + 1
        else:
            # 처음 받은 것은 '바뀜'으로 세지 않는다. 바뀌는 것을 본 캠만 '지금 화면'으로 믿는다
            meta["changes"] = meta.get("changes", 0) + 1 if meta.get("hash") else 0
            meta.update(hash=digest, url=used, changedAt=at, same=0,
                        etag=headers.get("ETag"), wide=used.endswith("maxresdefault_live.jpg") or kind == "snapshot")
            thumb_bytes[cid] = body
            try:
                with open(thumb_path(cid), "wb") as f:
                    f.write(body)
            except OSError:
                pass
        if kind == "youtube":
            meta["static"] = meta.get("same", 0) >= STATIC_AFTER
        if kind == "snapshot" and headers is not None and headers.get("Last-Modified"):
            meta["takenAt"] = parsedate_to_datetime(headers["Last-Modified"]).isoformat(timespec="seconds")
    with L:
        S["thumbs"][cid] = meta
        if kind == "snapshot":
            taken = meta.get("takenAt")
            status = "link" if not taken else "fresh" if minutes_since(taken) <= SNAP_FRESH_MIN else "stale"
            prev = S["checks"].get(cid, {})
            S["checks"][cid] = {"camId": cid, "status": status, "takenAt": taken, "checkedAt": at}
            if prev.get("status") != status or minutes_since(prev.get("loggedAt")) >= 60:
                S["checks"][cid]["loggedAt"] = at
                add_stat(cid, status, at)
                append_line(LOG_FILE, {"camId": cid, "checkedAt": at, "status": status})
            else:
                S["checks"][cid]["loggedAt"] = prev.get("loggedAt")


def thumb_state(cam, meta):
    """미리보기가 '지금 화면'인지: live(5분마다 바뀌는 것을 확인) / fixed(안 바뀜) / unknown(지켜보는 중).
    유튜브는 일부 방송의 미리보기를 갱신하지 않거나, 채널이 홍보 사진을 걸어 두기도 한다."""
    if cam.get("type") == "snapshot":
        return "live"
    if cam.get("customThumb") or meta.get("static"):
        return "fixed"
    if meta.get("changes", 0) >= 1 and minutes_since(meta.get("changedAt")) <= THUMB_LIVE_MIN:
        return "live"
    return "unknown"


def safe_update_thumb(cam):
    try:
        update_thumb(cam)
    except Exception as e:
        print("썸네일 갱신 실패:", cam.get("id"), e)


def get_thumb_bytes(cid):
    if cid in thumb_bytes:
        return thumb_bytes[cid]
    try:
        with open(thumb_path(cid), "rb") as f:
            thumb_bytes[cid] = f.read()
            return thumb_bytes[cid]
    except OSError:
        return None


def obs_worker():
    """공항 관측을 주기에 맞춰 갱신한다. (처음에는 가까운 공항을 찾느라 1분쯤 걸린다)"""
    while True:
        try:
            update_obs()
            refresh_snow()
            save_state()
        except Exception as e:
            print("공항 관측 갱신 실패:", e)
        time.sleep(WEATHER_MIN * 60)


def data_worker():
    """예보와 썸네일을 주기에 맞춰 갱신한다."""
    weather_at = 0.0
    while True:
        try:
            now = time.time()
            if now - weather_at >= WEATHER_MIN * 60:
                weather_at = now
                try:
                    update_weather()
                except Exception as e:
                    print("날씨 갱신 실패:", e)
                refresh_snow()
                save_state()
            watched = now - last_poll < WATCH_IDLE_MIN * 60
            due = []
            for cam in load_cams():
                kind = cam.get("type", "youtube")
                if kind not in ("youtube", "snapshot"):
                    continue
                if kind == "youtube" and not watched:
                    continue
                every = SNAP_MIN if kind == "snapshot" else THUMB_MIN
                if minutes_since(S["thumbs"].get(cam["id"], {}).get("fetchedAt")) >= every:
                    due.append(cam)
            if due:  # 썸네일은 유튜브 이미지 서버(CDN)에서 받으므로 네 개씩 함께 받아도 된다
                with ThreadPoolExecutor(max_workers=4) as ex:
                    list(ex.map(safe_update_thumb, due))
                save_state()
            time.sleep(5)
        except Exception as e:
            print("자료 갱신 오류:", e)
            time.sleep(15)


# ---- 눈 대조: 예보 · 관측 · 화면 · 사람 ---------------------------------------
def judge_screen(cid):
    """화면 분석 수치를 그 캠의 '눈 없는 평소 값'과 비교한다. (실험적)"""
    v = S["vision"].get(cid)
    if not v or minutes_since(v.get("at")) > VISION_VALID_MIN:
        return None
    cam = next((c for c in load_cams() if c["id"] == cid), {})
    if thumb_state(cam, S["thumbs"].get(cid, {})) != "live":
        return {"fall": None, "cover": None, "learned": False, "why": "미리보기가 지금 화면인지 아직 확인되지 않음"}
    base =S["baseline"].get(cid, {}).get("day" if v.get("isDay") else "night", {})
    learned = base.get("cellsN", 0) >= FALL_LEARN_N
    out = {"fall": None, "cover": None, "learned": learned, "why": None}
    if not v.get("hasPrev"):
        out["why"] = "비교할 다음 화면을 기다리는 중"
    elif v.get("moved"):
        out["why"] = "카메라가 움직였거나 장면이 크게 바뀜"
    elif not learned:
        out["why"] = f'이 캠의 평소 값을 배우는 중 ({base.get("cellsN", 0)}/{FALL_LEARN_N})'
    else:
        # 평소에도 새 점이 많이 생기는 화면(김·물결·인파)은 기준이 높아지고, 너무 높으면 판정하지 않는다
        thr = max(FALL_MIN_CELLS, base["cells"] * 2 + 0.10, base.get("cellsPeak", 0) * 1.15 + 0.05)
        if thr > FALL_MAX_THR:
            out["why"] = "평소에도 화면 변화가 많아 눈발을 가릴 수 없음"
        else:
            out.update(fall=v.get("cells", 0) >= thr, fallThr=round(thr, 2))
    if v.get("isDay"):  # 밤에는 조명 때문에 흰 면적으로 쌓인 눈을 가릴 수 없다
        thr = base["white"] + COVER_OVER_BASE if base.get("n", 0) >= 3 and "white" in base else COVER_UNCAL_WHITE
        out.update(cover=v.get("white", 0) >= thr, coverThr=round(thr, 2))
    return out


def snow_verdict(cam):
    cid = cam["id"]
    w, o = S["weather"].get(cid), S["obs"].get(cid)
    model = bool(w and (w["code"] in SNOW_CODES or w["snowfall"] > 0))
    obs_ok = bool(o and (o.get("km") or 999) <= OBS_MAX_KM and minutes_since(o.get("time")) <= OBS_MAX_AGE_MIN)
    obs_snow = obs_ok and o["snow"]
    screen = judge_screen(cid)
    screen_fall = bool(screen and screen.get("fall"))
    label = S["labels"].get(cid)
    eye = label["seen"] if label and minutes_since(label.get("at")) <= EYE_VALID_MIN else None

    if eye is True:
        level, text = "confirmed", "사람이 화면에서 눈을 확인했어요"
    elif eye is False:
        level, text = "none", "사람이 확인: 화면에 눈이 안 보여요"
    elif model + obs_snow + screen_fall >= 2:
        names = [n for n, on in (("예보", model), ("공항 관측", obs_snow), ("화면", screen_fall)) if on]
        level, text = "confirmed", "·".join(names) + "에서 모두 눈이에요"
    elif model and obs_ok and not o["snow"] and (o["rain"] or (o.get("t") or 0) >= 3):
        level, text = "disputed", "예보는 눈인데 공항 관측은 " + ("비예요" if o["rain"] else "눈이 아니에요")
    elif model:
        level, text = "likely", "예보로는 눈이 오고 있어요 (다른 근거는 아직 없음)"
    elif obs_snow:
        level, text = "likely", f'공항({o["icao"]}, {o["km"]}km)에서 눈을 관측했어요'
    elif screen_fall:
        level, text = "screen", "화면에서만 눈발 같은 움직임이 보여요 (오탐 가능)"
    else:
        level, text = "none", ""

    depth_cm = round((w["depth"] if w else 0) * 100, 1)
    screen_cover = bool(screen and screen.get("cover"))
    cover = "both" if depth_cm >= 1 and screen_cover else "model" if depth_cm >= 1 else "screen" if screen_cover else None
    return {
        "level": level, "text": text, "cover": cover, "depthCm": depth_cm,
        "model": model, "obs": (obs_snow if obs_ok else None), "screen": screen, "eye": eye,
    }


def refresh_snow():
    """판정을 다시 계산하고, 바뀐 곳은 눈 일지에 적는다."""
    at = now_iso()
    with L:
        for cam in load_cams():
            if cam.get("relay") or cam.get("lat") is None:
                continue
            cid = cam["id"]
            new = snow_verdict(cam)
            old = S["snow"].get(cid, {})
            S["snow"][cid] = new
            if old.get("level", "none") != new["level"]:
                ev = {"camId": cid, "city": cam.get("city"), "at": at, "from": old.get("level", "none"),
                      "level": new["level"], "text": new["text"]}
                S["events"] = ([ev] + S["events"])[:40]
                append_line(SNOW_LOG_FILE, {**ev, "weather": S["weather"].get(cid), "obs": S["obs"].get(cid),
                                            "vision": S["vision"].get(cid)})


def record_vision(v):
    """브라우저가 보낸 화면 분석 수치를 저장하고, 눈 없는 때의 값은 평소 값으로 배운다."""
    cid = v.get("camId")
    if not cid:
        return
    keep = {k: v.get(k) for k in ("hash", "isDay", "sky", "white", "specks", "transient", "cells",
                                  "retention", "diff", "hasPrev", "moved")}
    keep["at"] = now_iso()
    with L:
        S["vision"][cid] = keep
        w, o = S["weather"].get(cid), S["obs"].get(cid)
        cam = next((c for c in load_cams() if c["id"] == cid), {})
        quiet = (w is not None and w["code"] not in SNOW_CODES and w["snowfall"] == 0
                 and w["depth"] < 0.005 and not (o and o.get("snow"))
                 and thumb_state(cam, S["thumbs"].get(cid, {})) == "live")
        if quiet:
            b = S["baseline"].setdefault(cid, {}).setdefault("day" if keep["isDay"] else "night", {"n": 0})
            if keep.get("white") is not None:
                b["white"] = keep["white"] if "white" not in b else round(b["white"] * 0.8 + keep["white"] * 0.2, 4)
                b["n"] = b.get("n", 0) + 1
            if keep["hasPrev"] and not keep["moved"] and keep.get("cells") is not None:
                c = keep["cells"]
                b["cells"] = c if "cells" not in b else round(b["cells"] * 0.8 + c * 0.2, 4)
                b["cellsPeak"] = round(max(b.get("cellsPeak", 0) * 0.98, c), 4)  # 최고값은 천천히 잊는다
                b["cellsN"] = b.get("cellsN", 0) + 1
    refresh_snow()
    save_state()


# ---- HTTP --------------------------------------------------------------------
def parse_video_id(text):
    text = text.strip()
    m = re.search(r"(?:v=|youtu\.be/|/live/|/embed/|/shorts/)([\w-]{11})", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[\w-]{11}", text) else None


def build_state():
    cams = load_cams()
    with L:
        checks = dict(S["checks"])
        for c in cams:
            if c.get("type") == "link":
                checks[c["id"]] = {"camId": c["id"], "status": "link"}
        return {
            "now": now_iso(),
            "cams": cams,
            "checks": checks,
            "weather": S["weather"],
            "obs": S["obs"],
            "thumbs": {c["id"]: {"hash": m.get("hash"), "changedAt": m.get("changedAt"),
                                 "takenAt": m.get("takenAt"), "state": thumb_state(c, m)}
                       for c in cams for m in [S["thumbs"].get(c["id"])] if m},
            "vision": S["vision"],
            "snow": S["snow"],
            "events": S["events"][:12],
            "verdicts": load_json(VERDICT_FILE, {}),
            "pins": load_json(PIN_FILE, []),
            "stats": stats,
            "blockedMin": max(0, round((S["blockedUntil"] - time.time()) / 60)),
        }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        # 화면 파일을 고친 뒤 브라우저가 옛 버전을 쓰지 않게 한다
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, obj, code=200):
        with L:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        global last_poll
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path.startswith("/data/"):  # 기록 파일은 화면에서 직접 열지 않는다
            return self.send_json({"error": "not found"}, 404)
        if u.path == "/api/state":
            last_poll = time.time()
            return self.send_json(build_state())
        if u.path == "/api/thumb":
            cid = q.get("id", [""])[0]
            cam = next((c for c in load_cams() if c["id"] == cid), None)
            if not cam:
                return self.send_json({"error": "no such cam"}, 404)
            img = get_thumb_bytes(cid)
            if img is None:
                try:
                    update_thumb(cam)
                except Exception:
                    pass
                img = get_thumb_bytes(cid)
            if img is None:
                return self.send_json({"error": "no thumb"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            # 주소에 썸네일 지문(v)이 붙어 있으면 내용이 바뀌지 않으므로 오래 캐시한다
            self.send_header("Cache-Control", "public, max-age=604800, immutable" if q.get("v") else "no-store")
            self.send_header("Content-Length", str(len(img)))
            self.end_headers()
            self.wfile.write(img)
            return
        return super().do_GET()

    def do_POST(self):
        try:
            self.handle_post()
        except Exception as e:  # 화면에 이유를 알려 주고 서버는 계속 돈다
            self.send_json({"error": str(e)[:160] or type(e).__name__}, 500)

    def handle_post(self):
        u = urllib.parse.urlparse(self.path)
        body = self.read_body()
        if u.path == "/api/verdict":
            v = load_json(VERDICT_FILE, {})
            if body.get("verdict"):
                v[body["camId"]] = {"verdict": body["verdict"], "at": now_iso()}
            else:
                v.pop(body["camId"], None)
            save_json(VERDICT_FILE, v)
            return self.send_json(v)
        if u.path == "/api/pin":
            pins = [p for p in load_json(PIN_FILE, []) if p != body["camId"]]
            if body.get("pinned"):
                pins.append(body["camId"])
            save_json(PIN_FILE, pins)
            return self.send_json(pins)
        if u.path == "/api/recheck":
            with L:
                if body.get("camId") not in forced:
                    forced.append(body.get("camId"))
            return self.send_json({"ok": True, "blockedMin": max(0, round((S["blockedUntil"] - time.time()) / 60))})
        if u.path == "/api/vision":
            record_vision(body)
            return self.send_json({"ok": True})
        if u.path == "/api/snow-label":
            cid = body.get("camId")
            with L:
                if body.get("seen") is None:
                    S["labels"].pop(cid, None)
                else:
                    S["labels"][cid] = {"seen": bool(body["seen"]), "at": now_iso()}
                    # 사람이 본 결과는 화면 분석 기준을 다듬을 때 쓰는 정답 자료다
                    append_line(SNOW_LOG_FILE, {"camId": cid, "at": now_iso(), "eye": bool(body["seen"]),
                                                "weather": S["weather"].get(cid), "obs": S["obs"].get(cid),
                                                "vision": S["vision"].get(cid)})
            refresh_snow()
            save_state(force=True)
            return self.send_json({"ok": True})
        if u.path == "/api/cams":
            vid = parse_video_id(body.get("url", ""))
            if not vid:
                return self.send_json({"error": "유튜브 주소나 영상 ID를 알아볼 수 없어요"}, 400)
            cams = list(load_cams())
            if any(c.get("videoId") == vid for c in cams):
                return self.send_json({"error": "이미 목록에 있는 영상이에요"}, 400)
            channel_id = None
            if time.time() >= S["blockedUntil"]:
                try:
                    info = inspect_video(vid)
                    channel_id = info.get("channelId")
                    title = info.get("title") or ""
                except Exception:
                    title = ""
            else:
                title = ""
            cam = {
                "id": "c" + str(int(time.time())),
                "city": body.get("city") or "?",
                "country": body.get("country") or "",
                "place": body.get("place") or title[:40],
                "lat": body.get("lat"),
                "lon": body.get("lon"),
                "videoId": vid,
                "channelId": channel_id,
                "note": "직접 추가",
            }
            cams.append(cam)
            save_json(CAMS_FILE, cams)
            with L:
                forced.append(cam["id"])
            return self.send_json(cam)
        if u.path == "/api/cams/delete":
            cams = [c for c in load_cams() if c["id"] != body.get("id")]
            save_json(CAMS_FILE, cams)
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)


def main():
    saved = load_json(STATE_FILE, {})
    for key in S:
        if key in saved and isinstance(saved[key], type(S[key])):
            S[key] = saved[key]
    load_stats()
    # 윈도우에서는 주소 재사용을 켜 두면 같은 포트에 서버가 여러 개 뜰 수 있어서 끈다
    ThreadingHTTPServer.allow_reuse_address = False
    ThreadingHTTPServer.daemon_threads = True
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"이미 SNOW WATCH 서버가 켜져 있어요. 브라우저에서 http://localhost:{PORT} 를 여세요.")
        print("서버를 새로 켜려면 기존 검은 창을 먼저 닫아 주세요.")
        sys.exit(1)
    threading.Thread(target=yt_worker, daemon=True).start()
    threading.Thread(target=data_worker, daemon=True).start()
    threading.Thread(target=obs_worker, daemon=True).start()
    print(f"SNOW WATCH 서버: http://localhost:{PORT}")
    print("이 창을 닫으면 서버가 꺼져요.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        save_state(force=True)


if __name__ == "__main__":
    main()
