# coding: utf-8
# ==============================================================================
# OTA QUICK SNIPER WEB
# 全国24場 簡易解析Web版
#
# 使い方:
#   Render等へ配置して、スマホはURLを開くだけ。
#
# 不変の技術仕様（単三黄金比）
# ==============================================================================
ENC_NAME = "utf-8"
ECL_NAME = "M"
TARGET_BYTES = 262

import re
import os
import json
import time
import math
import html
import datetime
import traceback
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from flask import Flask, render_template, request, jsonify

APP_VERSION = "1.5.3 WEB"
APP_NAME = "OTA QUICK SNIPER"
BASE_URL = "https://www.boatrace.jp/owpc/pc/race"
TIMEOUT = 15
RETRY = 2

app = Flask(__name__)

VENUES = {
    "01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島",
    "05": "多摩川", "06": "浜名湖", "07": "蒲郡", "08": "常滑",
    "09": "津", "10": "三国", "11": "びわこ", "12": "住之江",
    "13": "尼崎", "14": "鳴門", "15": "丸亀", "16": "児島",
    "17": "宮島", "18": "徳山", "19": "下関", "20": "若松",
    "21": "芦屋", "22": "福岡", "23": "唐津", "24": "大村",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "close",
}

CLASS_POINTS = {"A1": 12.0, "A2": 7.5, "B1": 3.0, "B2": 0.0}
LANE_POINTS = {1: 12.0, 2: 7.0, 3: 5.0, 4: 3.0, 5: 1.5, 6: 0.5}

CACHE = {}
CACHE_SECONDS = 90


def normalize_space(s):
    if s is None:
        return ""
    s = html.unescape(str(s)).replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n+", "\n", s)
    return s.strip()


def strip_tags(src):
    if not src:
        return ""
    s = re.sub(r"(?is)<script\b.*?</script>", " ", src)
    s = re.sub(r"(?is)<style\b.*?</style>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(?:td|th|tr|li|p|div|section|article|h1|h2|h3|h4)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return normalize_space(s)


def to_float(v, default=None):
    if v is None:
        return default
    s = normalize_space(v)
    if s in ("", "-", "－", "—"):
        return default
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return default
    try:
        return float(m.group())
    except Exception:
        return default


def to_int(v, default=None):
    x = to_float(v, None)
    if x is None:
        return default
    try:
        return int(x)
    except Exception:
        return default


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def http_get(url):
    last_error = None
    for attempt in range(RETRY + 1):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=TIMEOUT) as res:
                raw = res.read()
                for enc in ("utf-8", "cp932", "shift_jis"):
                    try:
                        return raw.decode(enc)
                    except Exception:
                        continue
                return raw.decode("utf-8", errors="replace")
        except Exception as e:
            last_error = e
            if attempt < RETRY:
                time.sleep(0.8)
    raise RuntimeError(f"通信失敗: {last_error}")


def make_url(page, hd, jcd, rno):
    q = urlencode({"hd": hd, "jcd": jcd, "rno": int(rno)})
    return f"{BASE_URL}/{page}?{q}"


def attr_value(tag, name):
    m = re.search(rf'(?is)\b{name}\s*=\s*["\']([^"\']*)["\']', tag or "")
    return html.unescape(m.group(1)) if m else ""


def extract_rows(src):
    return re.findall(r"(?is)<tr\b[^>]*>(.*?)</tr>", src or "")


def extract_cells(row_html):
    cells = re.findall(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html or "")
    return [normalize_space(strip_tags(c)) for c in cells]


def extract_tagged_cells(row_html):
    out = []
    for m in re.finditer(r"(?is)<t([dh])\b([^>]*)>(.*?)</t\1>", row_html or ""):
        out.append({
            "tag": m.group(1).lower(),
            "attrs": m.group(2),
            "html": m.group(3),
            "text": normalize_space(strip_tags(m.group(3))),
        })
    return out


RACER_HEAD_RE = re.compile(r"\b(\d{4})\s*/?\s*(A1|A2|B1|B2)\b")


def parse_racer_identity(row_text):
    m = RACER_HEAD_RE.search(row_text)
    if not m:
        return None
    regno = m.group(1)
    racer_class = m.group(2)
    after = normalize_space(row_text[m.end():])
    name = ""
    m2 = re.search(r"(.+?)\s+([^\s/]+/[^\s/]+)\s+\d+歳", after)
    if m2:
        name = normalize_space(m2.group(1))
    else:
        pieces = after.split()
        if pieces:
            name = pieces[0]
    return regno, racer_class, name


def parse_fixed_stats_from_cells(cells):
    joined = "\n".join(cells)
    ident = parse_racer_identity(joined)
    if not ident:
        return None

    regno, racer_class, name = ident
    racer = {
        "lane": None, "regno": regno, "class": racer_class, "name": name,
        "f": None, "l": None, "avg_st": None,
        "national_win": None, "national_2": None, "national_3": None,
        "local_win": None, "local_2": None, "local_3": None,
        "motor_no": None, "motor_2": None, "motor_3": None,
        "boat_no": None, "boat_2": None, "boat_3": None,
        "series_results": [], "series_st": [], "raw_cells": cells,
    }

    for c in cells[:3]:
        if re.fullmatch(r"[１-６1-6]", normalize_space(c)):
            racer["lane"] = int(c.translate(str.maketrans("１２３４５６", "123456")))
            break

    mf = re.search(r"\bF\s*([0-9]+)", joined)
    ml = re.search(r"\bL\s*([0-9]+)", joined)
    if mf: racer["f"] = int(mf.group(1))
    if ml: racer["l"] = int(ml.group(1))

    mst = re.search(r"\bL\s*[0-9]+\s+([01]\.\d{2})\b", joined)
    if mst:
        racer["avg_st"] = to_float(mst.group(1))

    identity_idx = None
    for i, c in enumerate(cells):
        if RACER_HEAD_RE.search(c):
            identity_idx = i
            break

    if identity_idx is not None:
        tail = cells[identity_idx + 1:]
        numeric_groups = []
        for c in tail:
            nums = re.findall(r"(?<!\d)(?:\d{1,3}(?:\.\d+)?)(?!\d)", c)
            if nums:
                numeric_groups.append((c, [to_float(x) for x in nums]))

        triples = []
        for c, nums in numeric_groups:
            if re.search(r"\bF\d|\bL\d", c):
                continue
            clean = [x for x in nums if x is not None]
            if len(clean) >= 3:
                triples.append(clean[:3])

        if len(triples) >= 4:
            nat, loc, mot, boat = triples[:4]
            racer["national_win"], racer["national_2"], racer["national_3"] = nat
            racer["local_win"], racer["local_2"], racer["local_3"] = loc
            racer["motor_no"] = int(mot[0]) if mot[0] is not None else None
            racer["motor_2"], racer["motor_3"] = mot[1], mot[2]
            racer["boat_no"] = int(boat[0]) if boat[0] is not None else None
            racer["boat_2"], racer["boat_3"] = boat[1], boat[2]

    if racer["national_win"] is None:
        tail_text = joined
        pos = tail_text.find(regno)
        if pos >= 0:
            tail_text = tail_text[pos:]
        pat = re.compile(
            r"F\s*(\d+)\s+L\s*(\d+)\s+"
            r"([01]\.\d{2})\s+"
            r"(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+"
            r"(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+"
            r"(\d{1,3})\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+"
            r"(\d{1,3})\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)"
        )
        m = pat.search(tail_text)
        if m:
            g = list(m.groups())
            racer["f"] = int(g[0]); racer["l"] = int(g[1]); racer["avg_st"] = to_float(g[2])
            racer["national_win"] = to_float(g[3]); racer["national_2"] = to_float(g[4]); racer["national_3"] = to_float(g[5])
            racer["local_win"] = to_float(g[6]); racer["local_2"] = to_float(g[7]); racer["local_3"] = to_float(g[8])
            racer["motor_no"] = to_int(g[9]); racer["motor_2"] = to_float(g[10]); racer["motor_3"] = to_float(g[11])
            racer["boat_no"] = to_int(g[12]); racer["boat_2"] = to_float(g[13]); racer["boat_3"] = to_float(g[14])

    return racer


def parse_series_from_row(row_html, racer):
    tagged = extract_tagged_cells(row_html)
    identity_idx = None
    for i, c in enumerate(tagged):
        if RACER_HEAD_RE.search(c["text"]):
            identity_idx = i
            break
    if identity_idx is None:
        return

    after = tagged[identity_idx + 1:]
    start = 0
    triple_count = 0
    for i, c in enumerate(after):
        nums = re.findall(r"(?<!\d)\d+(?:\.\d+)?(?!\d)", c["text"])
        if len(nums) >= 3 and not re.search(r"\bF\d|\bL\d", c["text"]):
            triple_count += 1
            if triple_count >= 4:
                start = i + 1
                break

    results, sts = [], []
    for c in after[start:]:
        txt = normalize_space(c["text"])
        for x in re.findall(r"(?<!\d)([1-6ＦＦFＬＬL])(?!\d)", txt):
            z = x.translate(str.maketrans("１２３４５６ＦＬ", "123456FL"))
            if len(results) < 12:
                results.append(z)
        for s in re.findall(r"(?<!\d)\.(\d{2})(?!\d)", txt):
            if len(sts) < 12:
                sts.append(float("0." + s))

    if 1 <= len(results) <= 10:
        racer["series_results"] = results
    if 1 <= len(sts) <= 10:
        racer["series_st"] = sts


def parse_racelist(src):
    racers, seen = [], set()
    for row in extract_rows(src):
        text = strip_tags(row)
        ident = parse_racer_identity(text)
        if not ident:
            continue
        regno = ident[0]
        if regno in seen:
            continue
        racer = parse_fixed_stats_from_cells(extract_cells(row))
        if not racer:
            continue
        parse_series_from_row(row, racer)
        if racer["lane"] is None:
            racer["lane"] = len(racers) + 1
        if 1 <= racer["lane"] <= 6:
            racers.append(racer)
            seen.add(regno)
    racers.sort(key=lambda x: x["lane"] or 99)
    return racers


def find_weather_value(text, label, unit=None):
    p = rf"{re.escape(label)}\s*([-+]?\d+(?:\.\d+)?)"
    if unit:
        p += rf"\s*{re.escape(unit)}"
    m = re.search(p, text)
    return to_float(m.group(1)) if m else None


def detect_weather_name(text):
    for w in ("晴", "曇り", "曇", "雨", "雪", "霧"):
        if re.search(rf"(?<!\w){w}(?!\w)", text):
            return w
    return ""


def detect_wind_direction(src, text):
    candidates = [
        "向かい風", "向い風", "追い風", "左横風", "右横風", "横風",
        "北北東", "東北東", "東南東", "南南東", "南南西", "西南西", "西北西", "北北西",
        "北東", "南東", "南西", "北西", "北", "東", "南", "西",
    ]
    combined = normalize_space(text + " " + html.unescape(src))
    for x in candidates:
        if x in combined:
            return x
    for tag in re.findall(r"(?is)<img\b[^>]*>", src):
        alt = normalize_space(attr_value(tag, "alt") + " " + attr_value(tag, "title"))
        for x in candidates:
            if x in alt:
                return x
    return ""


def parse_beforeinfo(src):
    text = strip_tags(src)
    data = {
        "weather": detect_weather_name(text),
        "air_temp": find_weather_value(text, "気温", "℃"),
        "water_temp": find_weather_value(text, "水温", "℃"),
        "wind_speed": find_weather_value(text, "風速", "m"),
        "wave_height": find_weather_value(text, "波高", "cm"),
        "wind_direction": detect_wind_direction(src, text),
        "racers": {}, "start_order": [], "stable_board": ("安定板" in text),
    }

    for row in extract_rows(src):
        cells = extract_cells(row)
        joined = " ".join(cells)
        ex = re.search(r"(?<!\d)([6-8]\.\d{2})(?!\d)", joined)
        if not ex:
            continue

        name = ""
        for c in cells:
            if re.search(r"[一-龯ぁ-んァ-ヶ]", c):
                if any(k in c for k in ("展示タイム", "チルト", "前走成績", "調整重量", "部品交換")):
                    continue
                if "kg" not in c and len(c) <= 30:
                    name = normalize_space(c)
                    break

        lane = None
        m_lane = re.search(r"(?:is-boatColor|boatColor|boat-color)[^0-9]{0,8}([1-6])", row, re.I)
        if m_lane:
            lane = int(m_lane.group(1))
        if lane is None:
            for c in cells[:2]:
                if re.fullmatch(r"[1-6１-６]", c):
                    lane = int(c.translate(str.maketrans("１２３４５６", "123456")))
                    break

        ex_time = to_float(ex.group(1))
        after_ex = joined[ex.end():]
        mt = re.search(r"(?<!\d)([-+]?(?:[0-3](?:\.\d)?))(?!\d)", after_ex)
        tilt = to_float(mt.group(1)) if mt else None
        mw = re.search(r"(\d{2}(?:\.\d)?)\s*kg", joined)
        weight = to_float(mw.group(1)) if mw else None

        key = lane if lane is not None else (len(data["racers"]) + 1)
        if 1 <= key <= 6 and key not in data["racers"]:
            data["racers"][key] = {
                "name": name, "weight": weight, "exhibition": ex_time, "tilt": tilt,
                "ex_st": None, "ex_course": None,
            }

    pos = text.find("スタート展示")
    if pos >= 0:
        tail = text[pos:pos + 1200]
        tokens = re.findall(r"(?<!\d)([1-6])\s*(F|L)?\s*\.?\s*(\d{2})(?!\d)", tail)
        used_lane = set()
        for course_idx, (lane_s, fl, st_s) in enumerate(tokens[:6], start=1):
            lane = int(lane_s)
            if lane in used_lane:
                continue
            used_lane.add(lane)
            st = float("0." + st_s)
            if fl == "F":
                st = -st
            data["start_order"].append(lane)
            if lane not in data["racers"]:
                data["racers"][lane] = {
                    "name": "", "weight": None, "exhibition": None, "tilt": None,
                    "ex_st": None, "ex_course": None,
                }
            data["racers"][lane]["ex_st"] = st
            data["racers"][lane]["ex_course"] = course_idx

    return data


def merge_data(racers, before):
    for r in racers:
        b = before.get("racers", {}).get(r["lane"], {})
        r["exhibition"] = b.get("exhibition")
        r["tilt"] = b.get("tilt")
        r["ex_st"] = b.get("ex_st")
        r["ex_course"] = b.get("ex_course")
        r["weight_before"] = b.get("weight")
    return racers


def norm_points(v, lo, hi, pts):
    if v is None or hi <= lo:
        return 0.0
    return clamp((float(v) - lo) / (hi - lo), 0.0, 1.0) * pts


def inverse_points(v, good, bad, pts):
    if v is None or bad <= good:
        return 0.0
    return clamp((bad - float(v)) / (bad - good), 0.0, 1.0) * pts


def series_form_points(results):
    vals = []
    for x in results or []:
        if str(x).isdigit():
            n = int(x)
            if 1 <= n <= 6:
                vals.append(n)
    if not vals:
        return 0.0, None
    vals = vals[-6:]
    avg = sum(vals) / len(vals)
    pts = clamp((6.0 - avg) / 5.0, 0.0, 1.0) * 8.0
    return pts, avg


def rank_bonus_by_value(racers, key, lower_is_better=True, max_pts=8.0):
    valid = [(r["lane"], r.get(key)) for r in racers if r.get(key) is not None]
    if not valid:
        return {}
    valid.sort(key=lambda x: x[1], reverse=not lower_is_better)
    n = len(valid)
    out = {}
    for idx, (lane, val) in enumerate(valid):
        out[lane] = max_pts if n <= 1 else max_pts * (1.0 - idx / (n - 1))
    return out


def wind_adjustment(lane, direction, speed):
    if speed is None or speed < 3:
        return 0.0
    d = direction or ""
    strength = clamp((float(speed) - 2.0) / 5.0, 0.0, 1.0)

    base = 0.0
    if lane == 1:
        base = -1.5 * strength
    elif lane in (2, 3):
        base = 0.8 * strength

    dir_adj = 0.0
    if "向" in d:
        if lane in (3, 4): dir_adj = 2.0 * strength
        if lane == 1: dir_adj = -1.0 * strength
    elif "追" in d:
        if lane in (1, 2): dir_adj = 1.5 * strength
        if lane in (5, 6): dir_adj = -0.5 * strength

    return base + dir_adj


def score_racers(racers, before):
    ex_rank = rank_bonus_by_value(racers, "exhibition", True, 10.0)

    st_source = [(r["lane"], abs(r["ex_st"])) for r in racers if r.get("ex_st") is not None]
    st_rank = {}
    if st_source:
        st_source.sort(key=lambda x: x[1])
        n = len(st_source)
        for idx, (lane, _) in enumerate(st_source):
            st_rank[lane] = 7.0 if n <= 1 else 7.0 * (1.0 - idx / (n - 1))

    HEAD_LANE_POINTS = {1: 30.0, 2: 15.0, 3: 10.0, 4: 6.0, 5: 2.0, 6: 0.0}
    PLACE_LANE_POINTS = {1: 12.0, 2: 11.0, 3: 10.0, 4: 8.0, 5: 6.0, 6: 4.0}

    for r in racers:
        class_p = CLASS_POINTS.get(r.get("class"), 0.0)
        national_win_p = norm_points(r.get("national_win"), 3.0, 8.0, 14.0)
        national_2_p = norm_points(r.get("national_2"), 15.0, 60.0, 7.0)
        national_3_p = norm_points(r.get("national_3"), 30.0, 80.0, 4.0)
        local_win_p = norm_points(r.get("local_win"), 3.0, 8.0, 6.0)
        local_2_p = norm_points(r.get("local_2"), 15.0, 60.0, 3.0)
        avg_st_p = inverse_points(r.get("avg_st"), 0.11, 0.24, 8.0)
        motor2_p = norm_points(r.get("motor_2"), 20.0, 55.0, 9.0)
        motor3_p = norm_points(r.get("motor_3"), 35.0, 75.0, 3.0)
        boat2_p = norm_points(r.get("boat_2"), 20.0, 55.0, 4.0)
        form_p, favg = series_form_points(r.get("series_results"))
        r["series_avg_finish"] = favg
        ex_p = ex_rank.get(r["lane"], 0.0)
        exst_p = st_rank.get(r["lane"], 0.0)
        f_pen = 3.0 if (r.get("f") or 0) >= 1 else 0.0
        exf_pen = 2.0 if (r.get("ex_st") is not None and r["ex_st"] < 0) else 0.0

        course_adj = 0.0
        if r.get("ex_course") is not None:
            diff = r["lane"] - r["ex_course"]
            if diff >= 2: course_adj = 6.0
            elif diff == 1: course_adj = 3.0
            elif diff <= -2: course_adj = -9.0
            elif diff == -1: course_adj = -6.0

        wind_adj = wind_adjustment(r["lane"], before.get("wind_direction"), before.get("wind_speed"))

        if before.get("stable_board"):
            if r["lane"] == 1: wind_adj -= 3.0
            elif r["lane"] in (2, 3): wind_adj += 1.5

        tilt = r.get("tilt")
        if tilt is not None:
            if tilt >= 1.0:
                if r["lane"] == 1: wind_adj -= 1.5
                elif r["lane"] in (2, 3, 4): wind_adj += 1.2
            elif tilt <= -0.5 and r["lane"] == 1:
                wind_adj += 1.0

        overall = (
            LANE_POINTS.get(r["lane"], 0.0) + class_p +
            national_win_p + national_2_p + national_3_p +
            local_win_p + local_2_p + avg_st_p +
            motor2_p + motor3_p + boat2_p +
            form_p + ex_p + exst_p + course_adj + wind_adj -
            f_pen - exf_pen
        )

        head = (
            HEAD_LANE_POINTS.get(r["lane"], 0.0) +
            class_p * 1.10 + national_win_p * 1.25 + national_2_p * 0.45 +
            local_win_p * 0.75 + avg_st_p * 1.15 +
            motor2_p * 0.40 + motor3_p * 0.20 + boat2_p * 0.20 +
            form_p * 0.65 + ex_p * 0.45 + exst_p * 0.55 +
            course_adj * 1.20 + wind_adj * 0.80 -
            f_pen * 1.30 - exf_pen
        )

        if r["lane"] == 4: head -= 2.0
        elif r["lane"] == 5: head -= 7.0
        elif r["lane"] == 6: head -= 10.0

        if r["lane"] == 1:
            if r.get("class") in ("A1", "A2"): head += 4.0
            if r.get("avg_st") is not None and r["avg_st"] <= 0.17: head += 2.0
            if r.get("national_win") is not None and r["national_win"] >= 5.5: head += 2.0

        place = (
            PLACE_LANE_POINTS.get(r["lane"], 0.0) +
            class_p * 0.75 + national_win_p * 0.70 +
            national_2_p * 1.05 + national_3_p * 1.15 +
            local_2_p * 0.70 + avg_st_p * 0.65 +
            motor2_p * 1.05 + motor3_p * 0.90 + boat2_p * 0.70 +
            form_p * 0.90 + ex_p * 0.95 + exst_p * 0.90 +
            course_adj + wind_adj - f_pen * 0.70 - exf_pen * 0.50
        )

        r["overall_raw"] = overall
        r["head_raw"] = head
        r["place_raw"] = place
        r["course_adj"] = course_adj

        reasons = []
        if r.get("class") == "A1": reasons.append("A1")
        if r.get("national_win") is not None and r["national_win"] >= 6.5: reasons.append("全国勝率上位")
        if r.get("avg_st") is not None and r["avg_st"] <= 0.14: reasons.append("平均ST良好")
        if r.get("motor_2") is not None and r["motor_2"] >= 40: reasons.append("モーター2連率良好")
        if favg is not None and favg <= 2.5: reasons.append("今節着順良好")
        if ex_p >= 8.0: reasons.append("展示上位")
        if exst_p >= 5.5: reasons.append("展示ST上位")
        if (r.get("f") or 0) >= 1: reasons.append("F持ち")
        if r.get("ex_st") is not None and r["ex_st"] < 0: reasons.append("展示F")
        r["reasons"] = reasons

    def scale_score(key, absolute_divisor):
        vals = [r[key] for r in racers]
        lo = min(vals) if vals else 0.0
        hi = max(vals) if vals else 1.0
        out_key = {"overall_raw":"overall_score", "head_raw":"head_score", "place_raw":"place_score"}[key]
        for r in racers:
            absolute = clamp(r[key] / absolute_divisor, 0.0, 1.0) * 60.0
            relative = 20.0 if hi == lo else 20.0 + ((r[key] - lo) / (hi - lo)) * 20.0
            r[out_key] = round(clamp(absolute + relative, 0.0, 100.0), 1)

    scale_score("overall_raw", 100.0)
    scale_score("head_raw", 90.0)
    scale_score("place_raw", 90.0)

    lane1_course_adj = next((r.get("course_adj") for r in racers if r["lane"] == 1), None)
    boat1_destabilized = lane1_course_adj is not None and lane1_course_adj < 0

    for r in racers:
        lane = r["lane"]
        cls = r.get("class")

        second = r["head_score"] * 0.55 + r["place_score"] * 0.45
        if lane == 2:
            second += 4.0
            if cls in ("A1", "A2"): second += 4.0
        elif lane == 3:
            second += 2.0
            if cls == "A1": second += 2.0
        elif lane == 4 and cls == "A1":
            second += 1.0
        if boat1_destabilized and lane in (2, 3):
            second += 5.0
        if (r.get("f") or 0) >= 1:
            second -= 2.0

        third = r["head_score"] * 0.22 + r["place_score"] * 0.78
        if lane == 5: third += 2.0
        elif lane == 6: third += 2.5
        if boat1_destabilized and lane in (2, 3):
            third += 3.0
        if r.get("motor_2") is not None and r["motor_2"] >= 40:
            third += 2.0

        r["second_score"] = round(clamp(second, 0.0, 100.0), 1)
        r["third_score"] = round(clamp(third, 0.0, 100.0), 1)
        r["score"] = r["overall_score"]

    racers.sort(key=lambda x: x["overall_score"], reverse=True)
    return racers


def data_coverage(racers, before):
    if not racers:
        return 0.0
    keys = [
        "class", "avg_st", "national_win", "national_2", "national_3",
        "local_win", "motor_2", "boat_2", "exhibition", "ex_st",
    ]
    got = 0
    total = len(racers) * len(keys)
    for r in racers:
        for k in keys:
            v = r.get(k)
            if v is not None and v != "":
                got += 1
    wk = ["weather", "wind_speed", "wave_height", "air_temp"]
    total += len(wk)
    for k in wk:
        v = before.get(k)
        if v is not None and v != "":
            got += 1
    return got / total if total else 0.0


def race_judgement(racers, before):
    if len(racers) < 3:
        return "⚫ 判定不能", 0, ["取得データ不足"]
    head_sorted = sorted(racers, key=lambda x: x.get("head_score", 0), reverse=True)
    hs = [r.get("head_score", 0) for r in head_sorted]
    top_gap = hs[0] - hs[1]
    third_gap = hs[0] - hs[2]
    cov = data_coverage(racers, before)

    confidence = 42.0 + clamp(top_gap, 0, 18) * 1.4 + clamp(third_gap, 0, 24) * 0.75 + cov * 22.0
    wind = before.get("wind_speed")
    wave = before.get("wave_height")
    notes = []

    if cov < 0.65:
        confidence -= 14; notes.append("取得データが少ない")
    if wind is not None and wind >= 7:
        confidence -= 8; notes.append("強風で不確定要素大")
    elif wind is not None and wind >= 5:
        confidence -= 4; notes.append("風が強め")
    if wave is not None and wave >= 8:
        confidence -= 5; notes.append("波高め")
    if before.get("stable_board"):
        confidence -= 4; notes.append("安定板使用")

    confidence = int(round(clamp(confidence, 0, 94)))
    if confidence >= 72 and cov >= 0.72:
        judge = "🟢 買い候補"
    elif confidence >= 58:
        judge = "🟡 慎重"
    else:
        judge = "🔴 見送り寄り"
    return judge, confidence, notes


def generate_bets(racers, max_bets=7):
    if len(racers) < 3:
        return []

    head_rank = sorted(racers, key=lambda x: x.get("head_score", 0), reverse=True)
    second_rank = sorted(racers, key=lambda x: x.get("second_score", 0), reverse=True)
    third_rank = sorted(racers, key=lambda x: x.get("third_score", 0), reverse=True)

    head_map = {r["lane"]: r.get("head_score", 0) for r in racers}
    second_map = {r["lane"]: r.get("second_score", 0) for r in racers}
    third_map = {r["lane"]: r.get("third_score", 0) for r in racers}
    racer_map = {r["lane"]: r for r in racers}

    first_pool = [r["lane"] for r in head_rank[:2]]
    filtered = []
    for lane in first_pool:
        if lane in (5, 6):
            top = head_map[lane]
            others = sorted([v for k, v in head_map.items() if k != lane], reverse=True)
            gap = top - (others[0] if others else 0)
            if head_rank[0]["lane"] == lane and gap >= 7.0:
                filtered.append(lane)
        else:
            filtered.append(lane)
    if not filtered:
        filtered = [head_rank[0]["lane"]]

    candidates = []
    for a in filtered:
        for b in [r["lane"] for r in second_rank[:5]]:
            if b == a: continue
            for c in [r["lane"] for r in third_rank[:5]]:
                if c in (a, b): continue
                val = head_map[a] * 1.28 + second_map[b] * 0.78 + third_map[c] * 0.52
                if a == 1 and b == 2:
                    val += 4.0
                    if racer_map[2].get("class") in ("A1", "A2"):
                        val += 3.0
                candidates.append((val, f"{a}-{b}-{c}"))

    candidates.sort(reverse=True)
    out, seen = [], set()
    for _, bet in candidates:
        if bet not in seen:
            out.append(bet); seen.add(bet)
        if len(out) >= max_bets:
            break

    if head_rank and head_rank[0]["lane"] == 1 and 2 in racer_map:
        r2 = racer_map[2]
        pos2 = next((i for i, r in enumerate(head_rank, 1) if r["lane"] == 2), 99)
        if r2.get("class") in ("A1", "A2") or pos2 <= 3:
            cands = [r["lane"] for r in third_rank if r["lane"] not in (1, 2)]
            if cands:
                safety = f"1-2-{cands[0]}"
                if safety not in out:
                    if len(out) >= max_bets:
                        out[-1] = safety
                    else:
                        out.append(safety)
    return out[:max_bets]


def analyze(jcd, rno, hd):
    key = f"{hd}:{jcd}:{rno}"
    now = time.time()
    cached = CACHE.get(key)
    if cached and now - cached["time"] <= CACHE_SECONDS:
        data = dict(cached["data"])
        data["cached"] = True
        return data

    racelist_url = make_url("racelist", hd, jcd, rno)
    before_url = make_url("beforeinfo", hd, jcd, rno)

    racers = parse_racelist(http_get(racelist_url))
    before = parse_beforeinfo(http_get(before_url))
    racers = merge_data(racers, before)

    if len(racers) < 3:
        raise RuntimeError("出走表を取得できません。未開催・公開前・HTML変更の可能性があります。")

    ranked = score_racers(racers, before)
    judge, confidence, notes = race_judgement(ranked, before)
    bets = generate_bets(ranked, 7)

    result = {
        "app_version": APP_VERSION,
        "venue_code": jcd,
        "venue": VENUES[jcd],
        "race": int(rno),
        "date": hd,
        "judge": judge,
        "confidence": confidence,
        "notes": notes,
        "bets": bets,
        "weather": before,
        "racers": sorted(ranked, key=lambda x: x["lane"]),
        "overall_rank": [r["lane"] for r in sorted(ranked, key=lambda x: x["overall_score"], reverse=True)],
        "head_rank": [r["lane"] for r in sorted(ranked, key=lambda x: x["head_score"], reverse=True)],
        "second_rank": [r["lane"] for r in sorted(ranked, key=lambda x: x["second_score"], reverse=True)],
        "third_rank": [r["lane"] for r in sorted(ranked, key=lambda x: x["third_score"], reverse=True)],
        "cached": False,
    }

    CACHE[key] = {"time": now, "data": result}
    return result


@app.route("/")
def index():
    today = datetime.datetime.now().strftime("%Y%m%d")
    return render_template("index.html", venues=VENUES, today=today, version=APP_VERSION)


@app.route("/api/analyze")
def api_analyze():
    try:
        jcd = str(request.args.get("jcd", "18")).zfill(2)
        rno = int(request.args.get("rno", "1"))
        hd = request.args.get("hd") or datetime.datetime.now().strftime("%Y%m%d")

        if jcd not in VENUES:
            return jsonify({"ok": False, "error": "場コードが不正です"}), 400
        if not (1 <= rno <= 12):
            return jsonify({"ok": False, "error": "レース番号は1～12です"}), 400
        if not re.fullmatch(r"\d{8}", hd):
            return jsonify({"ok": False, "error": "日付はYYYYMMDDです"}), 400

        data = analyze(jcd, rno, hd)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
