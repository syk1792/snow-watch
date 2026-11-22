"""한 번 돌고 끝나는 확인 실행기 (GitHub Actions 용). make_pages.py 가 pages/check.py 로 복사한다.

  python check.py

engine.py(= server.py)의 확인 로직을 그대로 쓴다.
  1) 유튜브 라이브 여부(캠마다 20분 이상 지난 것만, 한 번에 하나씩) 2) 예보 3) 공항 관측 4) 미리보기 5) 눈 판정
그리고 화면이 읽는 공개용 state.json 을 쓴다. 사람이 누르는 판정·화면 분석은 쓰지 않는다.
"""
import json
import os
import sys
import time

import engine as sv

sv.STATIC_AFTER = 3        # 30분 간격 확인이므로 3번(≈1.5시간) 그대로면 고정 이미지로 본다
sv.THUMB_LIVE_MIN = 75     # 75분 안에 미리보기가 바뀐 캠만 '지금 화면'으로 본다
RECHECK_MIN = 20           # 이보다 최근에 확인한 캠은 건너뛴다
HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(HERE, "state.json")


def log(*a):
    print(*a, flush=True)


def retry(fn, tries=3):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            log("   실패:", e)
            time.sleep(5 * (i + 1))


_RAW_KEY = os.environ.get("YT_API_KEY", "")
_m = __import__("re").search(r"AIza[0-9A-Za-z_-]{35}", _RAW_KEY)
API_KEY = _m.group(0) if _m else ""
if _RAW_KEY and not _m:
    print("YT_API_KEY 값에서 AIza로 시작하는 39자 키를 찾지 못했어요 (저장된 길이 %d, 앞 4글자 %r)" % (len(_RAW_KEY), _RAW_KEY[:4]), flush=True)
elif _RAW_KEY and len(_RAW_KEY.strip()) != len(API_KEY):
    print("YT_API_KEY 값에 키 말고 다른 글자가 섞여 있어요 (저장된 길이 %d, 키 부분 %d)" % (len(_RAW_KEY.strip()), len(API_KEY)), flush=True)


def api_round(cams):
    """유튜브 공식 API(videos.list)로 50개씩 한꺼번에 묻는다. 요청 1번에 쿼터 1."""
    import urllib.parse
    done = 0
    for i in range(0, len(cams), 50):
        part = cams[i:i + 50]
        ids = ",".join(c["videoId"] for c in part)
        url = ("https://www.googleapis.com/youtube/v3/videos?part=snippet,status,liveStreamingDetails&id="
               + ids + "&key=" + urllib.parse.quote(API_KEY))
        try:
            _, body, _ = sv.http_get(url, timeout=30)
        except Exception as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            msg = (str(e) + " " + detail).replace(API_KEY, "***").replace(chr(10), " ")
            log("   유튜브 API 실패:", msg[:400], "| 키 길이", len(API_KEY), "| 영상 ID 수", len(part))
            continue
        items = {v["id"]: v for v in json.loads(body).get("items", [])}
        at = sv.now_iso()
        for cam in part:
            v = items.get(cam["videoId"])
            if not v:
                res = {"videoId": cam["videoId"], "status": "unavailable", "title": "", "reason": "영상을 찾을 수 없음"}
            else:
                sn, lsd, stt = v.get("snippet", {}), v.get("liveStreamingDetails", {}), v.get("status", {})
                kind = sn.get("liveBroadcastContent")
                title = sn.get("title", "")
                if kind == "live":
                    status = "live"
                elif kind == "upcoming":
                    status = "upcoming"
                elif lsd.get("actualEndTime"):
                    status = "ended"
                else:
                    status = "not_live"
                res = {"videoId": cam["videoId"], "status": status, "title": title,
                       "author": sn.get("channelTitle"), "channelId": sn.get("channelId"),
                       "startTimestamp": lsd.get("actualStartTime"), "endTimestamp": lsd.get("actualEndTime"),
                       "watching": lsd.get("concurrentViewers"), "playableInEmbed": stt.get("embeddable"),
                       "reason": None, "suspectWords": [w for w in sv.SUSPECT_WORDS if w in title.lower()]}
            prev = sv.S["checks"].get(cam["id"], {})
            if prev.get("videoId") != res["videoId"]:
                sv.S["thumbs"].pop(cam["id"], None)
            sv.S["checks"][cam["id"]] = {**res, "camId": cam["id"], "checkedAt": at}
            done += 1
    return done


def youtube_round(cams):
    done = 0
    for cam in cams:
        if time.time() < sv.S["blockedUntil"]:
            log("유튜브가 막아서 남은 캠은 다음에 확인해요")
            break
        chk = sv.S["checks"].get(cam["id"], {})
        if chk.get("checkedAt") and sv.minutes_since(chk["checkedAt"]) < RECHECK_MIN:
            continue
        sv.run_youtube_check(cam)
        done += 1
        res = sv.S["checks"].get(cam["id"], {})
        log(f'  {cam["id"]:18s} {res.get("status")} {res.get("lastError", "") if res.get("status") == "error" else ""}')
        time.sleep(sv.YT_GAP_SEC)
    return done


def public_state():
    st = sv.build_state()
    cams, out = [], []
    for c in st["cams"]:
        c = dict(c)
        c.pop("note", None)
        cid = c["id"]
        chk = st["checks"].get(cid, {})
        # 녹화본·끝난 방송·재방송 의심 비교용 캠은 공개하지 않는다
        if cid.startswith("trap-") or cid.endswith("-walk") or chk.get("status") in ("ended", "not_live", "unavailable"):
            continue
        # 사진형은 다른 사이트의 사진이라 가져다 쓰지 않고 사이트 바로가기로만 둔다
        if c.get("type") == "snapshot":
            c["type"] = "link"
            c.pop("imageUrl", None)
            st["checks"][cid] = {"camId": cid, "status": "link"}
            st["thumbs"].pop(cid, None)
        # 퍼가기가 막힌 유튜브는 유튜브 바로가기로 둔다
        elif c.get("type", "youtube") == "youtube" and chk.get("status") == "live" and chk.get("playableInEmbed") is False:
            c["type"] = "link"
            c["pageUrl"] = "https://www.youtube.com/watch?v=" + (chk.get("videoId") or c.get("videoId", ""))
            c["source"] = "유튜브"
            st["checks"][cid] = {"camId": cid, "status": "link"}
        cams.append(c)
        out.append(cid)
    st["cams"] = cams
    keep = set(out)
    st["checks"] = {k: v for k, v in st["checks"].items() if k in keep}
    for k in ("weather", "obs", "snow"):
        st[k] = {i: v for i, v in st[k].items() if i in keep}
    # 미리보기 주소: 유튜브 이미지 서버 주소를 그대로 쓴다
    thumbs = {}
    for cid, t in st["thumbs"].items():
        meta = sv.S["thumbs"].get(cid, {})
        if cid in keep and meta.get("url", "").startswith("https://i.ytimg.com/"):
            thumbs[cid] = {**t, "url": meta["url"]}
    st["thumbs"] = thumbs
    st["pins"] = [p for p in st["pins"] if p in keep and p in thumbs]
    st["vision"], st["verdicts"], st["stats"] = {}, {}, {}
    wnote = sv.S.get("weatherNote") or {}
    wtimes = [v.get("fetchedAt") for v in sv.S["weather"].values() if v.get("fetchedAt")]
    st["meta"] = {"weatherTriedAt": wnote.get("at"), "weatherErrors": wnote.get("errors", []),
                  "weatherNewestAt": max(wtimes) if wtimes else None}
    st["events"] = [e for e in st["events"] if e.get("camId") in keep]
    return st


def main():
    saved = sv.load_json(sv.STATE_FILE, {})
    for key in sv.S:
        if key in saved and isinstance(saved[key], type(sv.S[key])):
            sv.S[key] = saved[key]
    cams = sv.load_cams()
    yt = [c for c in cams if c.get("type", "youtube") == "youtube" and c.get("videoId")]
    log(f"캠 {len(cams)}개 (유튜브 {len(yt)}개)")

    # 예전에 유튜브 봇 확인 화면에 막혀서 잘못 기록된 결과는 지운다
    for k in [k for k, v in sv.S["checks"].items() if "not a bot" in (v.get("reason") or "")]:
        del sv.S["checks"][k]
    log("1) 유튜브 라이브 확인")
    if API_KEY:
        n = api_round(yt)
    else:
        log("   YT_API_KEY 가 없어서 유튜브 확인은 건너뛰어요 (GitHub 서버에서는 유튜브 페이지를 직접 읽을 수 없어요)")
        n = 0 if os.environ.get("GITHUB_ACTIONS") else youtube_round(yt)
    log(f"   {n}개 확인")

    log("2) 날씨 예보")
    try:
        sv.update_weather()          # 안에서 20곳씩 3번까지 다시 시도한다
    except Exception as e:
        log("   날씨 실패:", e)
    log("3) 공항 관측")
    retry(sv.update_obs)
    log("4) 미리보기")
    from concurrent.futures import ThreadPoolExecutor
    due = [c for c in cams if c.get("type", "youtube") in ("youtube", "snapshot")]
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(sv.safe_update_thumb, due))
    sv.refresh_snow()
    sv.save_state(force=True)

    pub = public_state()
    with open(PUBLIC, "w", encoding="utf-8") as f:
        json.dump(pub, f, ensure_ascii=False, separators=(",", ":"))
    live = sum(1 for v in pub["checks"].values() if v.get("status") == "live")
    log(f"5) 저장: 캠 {len(pub['cams'])}개, 실시간 {live}개, 눈 표시 "
        f"{sum(1 for v in pub['snow'].values() if v.get('level') != 'none')}곳")


if __name__ == "__main__":
    sys.exit(main())
