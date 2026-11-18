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

    log("1) 유튜브 라이브 확인")
    n = youtube_round(yt)
    log(f"   {n}개 확인")

    log("2) 날씨 예보")
    retry(sv.update_weather)
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
