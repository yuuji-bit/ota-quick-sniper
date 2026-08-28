# coding: utf-8
ENC_NAME="utf-8"
ECL_NAME="M"
TARGET_BYTES=262

import re,os,json,time,html,datetime,math,hashlib,hmac
from functools import wraps
from urllib.request import Request,urlopen
from urllib.parse import urlencode
from flask import Flask,render_template,request,jsonify,Response,session,redirect,url_for

APP_VERSION="1.9.2 EV-PRIVATE+VERIFY+ODDS-DESKTOP-FIX"
APP_NAME="OTA QUICK SNIPER"
BASE_URL="https://www.boatrace.jp/owpc/pc/race"
TIMEOUT=15
RETRY=2

app=Flask(__name__)

# --- EV専用ページ認証 ---
# RenderのEnvironmentに EV_PASSWORD を設定するだけで使える。
EV_PASSWORD=os.environ.get("EV_PASSWORD","")
EV_LOGIN_DAYS=int(os.environ.get("EV_LOGIN_DAYS","90") or 90)
EV_COOKIE_SECURE=os.environ.get("EV_COOKIE_SECURE","1")!="0"
_session_seed=("ota-quick-sniper-ev:"+EV_PASSWORD).encode("utf-8") if EV_PASSWORD else os.urandom(32)
app.secret_key=hashlib.sha256(_session_seed).digest()
app.permanent_session_lifetime=datetime.timedelta(days=EV_LOGIN_DAYS)
app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE="Lax",SESSION_COOKIE_SECURE=EV_COOKIE_SECURE)

def ev_auth_ok():
    return bool(session.get("ev_auth"))

def ev_login_required(fn):
    @wraps(fn)
    def wrapped(*args,**kwargs):
        if not ev_auth_ok():
            if request.path.startswith("/api/"):
                return jsonify({"ok":False,"error":"EV専用ページへのログインが必要です"}),401
            return redirect(url_for("ev_login",next=request.full_path.rstrip("?")))
        return fn(*args,**kwargs)
    return wrapped

VENUES={
"01":"桐生","02":"戸田","03":"江戸川","04":"平和島","05":"多摩川","06":"浜名湖",
"07":"蒲郡","08":"常滑","09":"津","10":"三国","11":"びわこ","12":"住之江",
"13":"尼崎","14":"鳴門","15":"丸亀","16":"児島","17":"宮島","18":"徳山",
"19":"下関","20":"若松","21":"芦屋","22":"福岡","23":"唐津","24":"大村"}

HEADERS={
"User-Agent":"Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
"Accept-Language":"ja-JP,ja;q=0.9",
"Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
"Connection":"close"}

CLASS_POINTS={"A1":12.0,"A2":7.5,"B1":3.0,"B2":0.0}
LANE_POINTS={1:12.0,2:7.0,3:5.0,4:3.0,5:1.5,6:0.5}

CACHE={}
CACHE_SECONDS=90

VERIFY_STAKE_YEN=200
VERIFY_UPDATE_LIMIT=20
SUPABASE_PAGE_SIZE=1000
VERIFY_DIR=os.environ.get("OTA_DATA_DIR",os.path.join(os.path.dirname(__file__),"ota_verify_data"))
VERIFY_FILE=os.path.join(VERIFY_DIR,"analysis_log.json")
SUPABASE_URL=os.environ.get("SUPABASE_URL","").rstrip("/")
SUPABASE_KEY=os.environ.get("SUPABASE_KEY","")
SUPABASE_TABLE=os.environ.get("SUPABASE_TABLE","ota_quick_logs")

os.makedirs(VERIFY_DIR,exist_ok=True)

def normalize_space(s):
    if s is None:return ""
    s=html.unescape(str(s)).replace("\u3000"," ").replace("\xa0"," ")
    s=re.sub(r"[ \t\r\f\v]+"," ",s)
    s=re.sub(r"\n+","\n",s)
    return s.strip()

def strip_tags(src):
    if not src:return ""
    s=re.sub(r"(?is)<script\b.*?</script>"," ",src)
    s=re.sub(r"(?is)<style\b.*?</style>"," ",s)
    s=re.sub(r"(?i)<br\s*/?>","\n",s)
    s=re.sub(r"(?i)</(?:td|th|tr|li|p|div|section|article|h1|h2|h3|h4)>","\n",s)
    s=re.sub(r"(?s)<[^>]+>"," ",s)
    return normalize_space(s)

def to_float(v,default=None):
    if v is None:return default
    s=normalize_space(v)
    if s in ("","-","－","—"):return default
    m=re.search(r"[-+]?\d+(?:\.\d+)?",s)
    if not m:return default
    try:return float(m.group())
    except:return default

def to_int(v,default=None):
    x=to_float(v,None)
    if x is None:return default
    try:return int(x)
    except:return default

def clamp(x,lo,hi):return max(lo,min(hi,x))

def http_get(url):
    last=None
    for attempt in range(RETRY+1):
        try:
            req=Request(url,headers=HEADERS)
            with urlopen(req,timeout=TIMEOUT) as res:
                raw=res.read()
                for enc in ("utf-8","cp932","shift_jis"):
                    try:return raw.decode(enc)
                    except:pass
                return raw.decode("utf-8",errors="replace")
        except Exception as e:
            last=e
            if attempt<RETRY:time.sleep(.8)
    raise RuntimeError(f"通信失敗: {last}")

def make_url(page,hd,jcd,rno):
    return f"{BASE_URL}/{page}?"+urlencode({"hd":hd,"jcd":jcd,"rno":int(rno)})

def attr_value(tag,name):
    m=re.search(rf'(?is)\b{name}\s*=\s*["\']([^"\']*)["\']',tag or "")
    return html.unescape(m.group(1)) if m else ""

def extract_rows(src):return re.findall(r"(?is)<tr\b[^>]*>(.*?)</tr>",src or "")

def extract_cells(row_html):
    cells=re.findall(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>",row_html or "")
    return [normalize_space(strip_tags(c)) for c in cells]

def extract_tagged_cells(row_html):
    out=[]
    for m in re.finditer(r"(?is)<t([dh])\b([^>]*)>(.*?)</t\1>",row_html or ""):
        out.append({"text":normalize_space(strip_tags(m.group(3)))})
    return out

RACER_HEAD_RE=re.compile(r"\b(\d{4})\s*/?\s*(A1|A2|B1|B2)\b")

def parse_racer_identity(row_text):
    m=RACER_HEAD_RE.search(row_text)
    if not m:return None
    regno,cls=m.group(1),m.group(2)
    after=normalize_space(row_text[m.end():])
    name=""
    m2=re.search(r"(.+?)\s+([^\s/]+/[^\s/]+)\s+\d+歳",after)
    if m2:name=normalize_space(m2.group(1))
    else:
        p=after.split()
        if p:name=p[0]
    return regno,cls,name

def parse_fixed_stats_from_cells(cells):
    joined="\n".join(cells)
    ident=parse_racer_identity(joined)
    if not ident:return None
    regno,cls,name=ident
    r={"lane":None,"regno":regno,"class":cls,"name":name,"f":None,"l":None,"avg_st":None,
       "national_win":None,"national_2":None,"national_3":None,"local_win":None,"local_2":None,"local_3":None,
       "motor_no":None,"motor_2":None,"motor_3":None,"boat_no":None,"boat_2":None,"boat_3":None,
       "series_results":[],"series_st":[],"raw_cells":cells}
    for c in cells[:3]:
        if re.fullmatch(r"[１-６1-6]",normalize_space(c)):
            r["lane"]=int(c.translate(str.maketrans("１２３４５６","123456")));break
    mf=re.search(r"\bF\s*([0-9]+)",joined);ml=re.search(r"\bL\s*([0-9]+)",joined)
    if mf:r["f"]=int(mf.group(1))
    if ml:r["l"]=int(ml.group(1))
    mst=re.search(r"\bL\s*[0-9]+\s+([01]\.\d{2})\b",joined)
    if mst:r["avg_st"]=to_float(mst.group(1))
    idx=next((i for i,c in enumerate(cells) if RACER_HEAD_RE.search(c)),None)
    if idx is not None:
        groups=[]
        for c in cells[idx+1:]:
            nums=re.findall(r"(?<!\d)(?:\d{1,3}(?:\.\d+)?)(?!\d)",c)
            if nums:groups.append((c,[to_float(x) for x in nums]))
        triples=[]
        for c,nums in groups:
            if re.search(r"\bF\d|\bL\d",c):continue
            clean=[x for x in nums if x is not None]
            if len(clean)>=3:triples.append(clean[:3])
        if len(triples)>=4:
            nat,loc,mot,boat=triples[:4]
            r["national_win"],r["national_2"],r["national_3"]=nat
            r["local_win"],r["local_2"],r["local_3"]=loc
            r["motor_no"]=int(mot[0]);r["motor_2"],r["motor_3"]=mot[1],mot[2]
            r["boat_no"]=int(boat[0]);r["boat_2"],r["boat_3"]=boat[1],boat[2]
    if r["national_win"] is None:
        tail=joined[joined.find(regno):]
        pat=re.compile(
        r"F\s*(\d+)\s+L\s*(\d+)\s+([01]\.\d{2})\s+"
        r"(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+"
        r"(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+"
        r"(\d{1,3})\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+"
        r"(\d{1,3})\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)")
        m=pat.search(tail)
        if m:
            g=list(m.groups())
            r["f"]=int(g[0]);r["l"]=int(g[1]);r["avg_st"]=to_float(g[2])
            r["national_win"]=to_float(g[3]);r["national_2"]=to_float(g[4]);r["national_3"]=to_float(g[5])
            r["local_win"]=to_float(g[6]);r["local_2"]=to_float(g[7]);r["local_3"]=to_float(g[8])
            r["motor_no"]=to_int(g[9]);r["motor_2"]=to_float(g[10]);r["motor_3"]=to_float(g[11])
            r["boat_no"]=to_int(g[12]);r["boat_2"]=to_float(g[13]);r["boat_3"]=to_float(g[14])
    return r

def parse_series_from_row(row_html,r):
    tagged=extract_tagged_cells(row_html)
    idx=next((i for i,c in enumerate(tagged) if RACER_HEAD_RE.search(c["text"])),None)
    if idx is None:return
    after=tagged[idx+1:];start=0;cnt=0
    for i,c in enumerate(after):
        nums=re.findall(r"(?<!\d)\d+(?:\.\d+)?(?!\d)",c["text"])
        if len(nums)>=3 and not re.search(r"\bF\d|\bL\d",c["text"]):
            cnt+=1
            if cnt>=4:start=i+1;break
    results=[]
    for c in after[start:]:
        txt=normalize_space(c["text"])
        for x in re.findall(r"(?<!\d)([1-6ＦＦFＬＬL])(?!\d)",txt):
            z=x.translate(str.maketrans("１２３４５６ＦＬ","123456FL"))
            if len(results)<12:results.append(z)
    if 1<=len(results)<=10:r["series_results"]=results

def parse_racelist(src):
    racers=[];seen=set()
    for row in extract_rows(src):
        ident=parse_racer_identity(strip_tags(row))
        if not ident or ident[0] in seen:continue
        r=parse_fixed_stats_from_cells(extract_cells(row))
        if not r:continue
        parse_series_from_row(row,r)
        if r["lane"] is None:r["lane"]=len(racers)+1
        if 1<=r["lane"]<=6:
            racers.append(r);seen.add(ident[0])
    racers.sort(key=lambda x:x["lane"] or 99)
    return racers

def find_weather_value(text,label,unit=None):
    p=rf"{re.escape(label)}\s*([-+]?\d+(?:\.\d+)?)"
    if unit:p+=rf"\s*{re.escape(unit)}"
    m=re.search(p,text)
    return to_float(m.group(1)) if m else None

def detect_weather_name(text):
    for w in ("晴","曇り","曇","雨","雪","霧"):
        if w in text:return w
    return ""

def detect_wind_direction(src,text):
    cand=["向かい風","向い風","追い風","左横風","右横風","横風","北北東","東北東","東南東","南南東",
          "南南西","西南西","西北西","北北西","北東","南東","南西","北西","北","東","南","西"]
    combined=normalize_space(text+" "+html.unescape(src))
    for x in cand:
        if x in combined:return x
    for tag in re.findall(r"(?is)<img\b[^>]*>",src):
        alt=normalize_space(attr_value(tag,"alt")+" "+attr_value(tag,"title"))
        for x in cand:
            if x in alt:return x
    return ""

def parse_beforeinfo(src):
    text=strip_tags(src)
    d={"weather":detect_weather_name(text),"air_temp":find_weather_value(text,"気温","℃"),
       "water_temp":find_weather_value(text,"水温","℃"),"wind_speed":find_weather_value(text,"風速","m"),
       "wave_height":find_weather_value(text,"波高","cm"),"wind_direction":detect_wind_direction(src,text),
       "racers":{},"start_order":[],"stable_board":("安定板" in text)}
    for row in extract_rows(src):
        cells=extract_cells(row);joined=" ".join(cells)
        ex=re.search(r"(?<!\d)([6-8]\.\d{2})(?!\d)",joined)
        if not ex:continue
        lane=None
        for c in cells[:2]:
            if re.fullmatch(r"[1-6１-６]",c):
                lane=int(c.translate(str.maketrans("１２３４５６","123456")));break
        key=lane if lane is not None else len(d["racers"])+1
        mt=re.search(r"(?<!\d)([-+]?(?:[0-3](?:\.\d)?))(?!\d)",joined[ex.end():])
        if 1<=key<=6 and key not in d["racers"]:
            d["racers"][key]={"exhibition":to_float(ex.group(1)),"tilt":to_float(mt.group(1)) if mt else None,
                              "ex_st":None,"ex_course":None}
    pos=text.find("スタート展示")
    if pos>=0:
        tail=text[pos:pos+1200]
        tokens=re.findall(r"(?<!\d)([1-6])\s*(F|L)?\s*\.?\s*(\d{2})(?!\d)",tail)
        used=set()
        for course,(ls,fl,ss) in enumerate(tokens[:6],1):
            lane=int(ls)
            if lane in used:continue
            used.add(lane);st=float("0."+ss)
            if fl=="F":st=-st
            d["start_order"].append(lane)
            d["racers"].setdefault(lane,{"exhibition":None,"tilt":None,"ex_st":None,"ex_course":None})
            d["racers"][lane]["ex_st"]=st;d["racers"][lane]["ex_course"]=course
    return d

def merge_data(racers,before):
    for r in racers:
        b=before.get("racers",{}).get(r["lane"],{})
        r["exhibition"]=b.get("exhibition");r["tilt"]=b.get("tilt")
        r["ex_st"]=b.get("ex_st");r["ex_course"]=b.get("ex_course")
    return racers

def norm_points(v,lo,hi,pts):
    if v is None or hi<=lo:return 0.0
    return clamp((float(v)-lo)/(hi-lo),0,1)*pts

def inverse_points(v,good,bad,pts):
    if v is None or bad<=good:return 0.0
    return clamp((bad-float(v))/(bad-good),0,1)*pts

def series_form_points(results):
    vals=[int(x) for x in (results or []) if str(x).isdigit() and 1<=int(x)<=6]
    if not vals:return 0.0,None
    vals=vals[-6:];avg=sum(vals)/len(vals)
    return clamp((6-avg)/5,0,1)*8,avg

def rank_bonus_by_value(racers,key,lower_is_better=True,max_pts=8):
    valid=[(r["lane"],r.get(key)) for r in racers if r.get(key) is not None]
    if not valid:return {}
    valid.sort(key=lambda x:x[1],reverse=not lower_is_better);n=len(valid)
    return {lane:(max_pts if n<=1 else max_pts*(1-i/(n-1))) for i,(lane,_) in enumerate(valid)}

def wind_adjustment(lane,direction,speed):
    if speed is None or speed<3:return 0
    d=direction or "";s=clamp((float(speed)-2)/5,0,1)
    base=-1.5*s if lane==1 else (.8*s if lane in (2,3) else 0)
    adj=0
    if "向" in d:
        if lane in (3,4):adj=2*s
        if lane==1:adj=-1*s
    elif "追" in d:
        if lane in (1,2):adj=1.5*s
        if lane in (5,6):adj=-.5*s
    return base+adj

def score_racers(racers,before):
    ex_rank=rank_bonus_by_value(racers,"exhibition",True,10)
    sts=sorted([(r["lane"],abs(r["ex_st"])) for r in racers if r.get("ex_st") is not None],key=lambda x:x[1])
    st_rank={}
    if sts:
        n=len(sts)
        for i,(lane,_) in enumerate(sts):st_rank[lane]=7 if n<=1 else 7*(1-i/(n-1))
    HP={1:30,2:15,3:10,4:6,5:2,6:0};PP={1:12,2:11,3:10,4:8,5:6,6:4}
    for r in racers:
        cp=CLASS_POINTS.get(r.get("class"),0)
        nw=norm_points(r.get("national_win"),3,8,14);n2=norm_points(r.get("national_2"),15,60,7)
        n3=norm_points(r.get("national_3"),30,80,4);lw=norm_points(r.get("local_win"),3,8,6)
        l2=norm_points(r.get("local_2"),15,60,3);st=inverse_points(r.get("avg_st"),.11,.24,8)
        m2=norm_points(r.get("motor_2"),20,55,9);m3=norm_points(r.get("motor_3"),35,75,3)
        b2=norm_points(r.get("boat_2"),20,55,4);form,favg=series_form_points(r.get("series_results"))
        ex=ex_rank.get(r["lane"],0);exst=st_rank.get(r["lane"],0)
        fp=3 if (r.get("f") or 0)>=1 else 0;ef=2 if r.get("ex_st") is not None and r["ex_st"]<0 else 0
        ca=0
        if r.get("ex_course") is not None:
            diff=r["lane"]-r["ex_course"]
            ca=6 if diff>=2 else 3 if diff==1 else -9 if diff<=-2 else -6 if diff==-1 else 0
        wa=wind_adjustment(r["lane"],before.get("wind_direction"),before.get("wind_speed"))
        if before.get("stable_board"):
            if r["lane"]==1:wa-=3
            elif r["lane"] in (2,3):wa+=1.5
        tilt=r.get("tilt")
        if tilt is not None:
            if tilt>=1:
                if r["lane"]==1:wa-=1.5
                elif r["lane"] in (2,3,4):wa+=1.2
            elif tilt<=-.5 and r["lane"]==1:wa+=1
        overall=LANE_POINTS.get(r["lane"],0)+cp+nw+n2+n3+lw+l2+st+m2+m3+b2+form+ex+exst+ca+wa-fp-ef
        head=HP[r["lane"]]+cp*1.1+nw*1.25+n2*.45+lw*.75+st*1.15+m2*.4+m3*.2+b2*.2+form*.65+ex*.45+exst*.55+ca*1.2+wa*.8-fp*1.3-ef
        if r["lane"]==4:head-=2
        elif r["lane"]==5:head-=7
        elif r["lane"]==6:head-=10
        if r["lane"]==1:
            if r.get("class") in ("A1","A2"):head+=4
            if r.get("avg_st") is not None and r["avg_st"]<=.17:head+=2
            if r.get("national_win") is not None and r["national_win"]>=5.5:head+=2
        place=PP[r["lane"]]+cp*.75+nw*.7+n2*1.05+n3*1.15+l2*.7+st*.65+m2*1.05+m3*.9+b2*.7+form*.9+ex*.95+exst*.9+ca+wa-fp*.7-ef*.5
        r["overall_raw"]=overall;r["head_raw"]=head;r["place_raw"]=place;r["course_adj"]=ca;r["series_avg_finish"]=favg
    def scale(k,out,div):
        vals=[r[k] for r in racers];lo=min(vals);hi=max(vals)
        for r in racers:
            absolute=clamp(r[k]/div,0,1)*60
            relative=20 if hi==lo else 20+((r[k]-lo)/(hi-lo))*20
            r[out]=round(clamp(absolute+relative,0,100),1)
    scale("overall_raw","overall_score",100);scale("head_raw","head_score",90);scale("place_raw","place_score",90)
    lane1=next((r.get("course_adj") for r in racers if r["lane"]==1),None);destab=lane1 is not None and lane1<0
    for r in racers:
        lane=r["lane"];cls=r.get("class")
        second=r["head_score"]*.55+r["place_score"]*.45
        if lane==2:second+=4+(4 if cls in ("A1","A2") else 0)
        elif lane==3:second+=2+(2 if cls=="A1" else 0)
        elif lane==4 and cls=="A1":second+=1
        if destab and lane in (2,3):second+=5
        if (r.get("f") or 0)>=1:second-=2
        third=r["head_score"]*.22+r["place_score"]*.78+(2 if lane==5 else 2.5 if lane==6 else 0)
        if destab and lane in (2,3):third+=3
        if r.get("motor_2") is not None and r["motor_2"]>=40:third+=2
        r["second_score"]=round(clamp(second,0,100),1);r["third_score"]=round(clamp(third,0,100),1);r["score"]=r["overall_score"]
    return sorted(racers,key=lambda x:x["overall_score"],reverse=True)

def data_coverage(racers,before):
    if not racers:return 0
    keys=["class","avg_st","national_win","national_2","national_3","local_win","motor_2","boat_2","exhibition","ex_st"]
    got=sum(1 for r in racers for k in keys if r.get(k) not in (None,""));total=len(racers)*len(keys)
    for k in ["weather","wind_speed","wave_height","air_temp"]:
        total+=1
        if before.get(k) not in (None,""):got+=1
    return got/total if total else 0

def race_judgement(racers,before):
    if len(racers)<3:return "⚫ 判定不能",0,["取得データ不足"]
    hs=sorted([r.get("head_score",0) for r in racers],reverse=True)
    cov=data_coverage(racers,before)
    confidence=42+clamp(hs[0]-hs[1],0,18)*1.4+clamp(hs[0]-hs[2],0,24)*.75+cov*22
    notes=[];wind=before.get("wind_speed");wave=before.get("wave_height")
    if cov<.65:confidence-=14;notes.append("取得データが少ない")
    if wind is not None and wind>=7:confidence-=8;notes.append("強風で不確定要素大")
    elif wind is not None and wind>=5:confidence-=4;notes.append("風が強め")
    if wave is not None and wave>=8:confidence-=5;notes.append("波高め")
    if before.get("stable_board"):confidence-=4;notes.append("安定板使用")
    confidence=int(round(clamp(confidence,0,94)))
    judge="🟢 買い候補" if confidence>=72 and cov>=.72 else "🟡 慎重" if confidence>=58 else "🔴 見送り寄り"
    return judge,confidence,notes

def generate_bets(racers,max_bets=7):
    if len(racers)<3:return []
    hr=sorted(racers,key=lambda x:x.get("head_score",0),reverse=True)
    sr=sorted(racers,key=lambda x:x.get("second_score",0),reverse=True)
    tr=sorted(racers,key=lambda x:x.get("third_score",0),reverse=True)
    hm={r["lane"]:r.get("head_score",0) for r in racers};sm={r["lane"]:r.get("second_score",0) for r in racers}
    tm={r["lane"]:r.get("third_score",0) for r in racers};rm={r["lane"]:r for r in racers}
    first=[r["lane"] for r in hr[:2]];filtered=[]
    for lane in first:
        if lane in (5,6):
            others=sorted([v for k,v in hm.items() if k!=lane],reverse=True)
            if hr[0]["lane"]==lane and hm[lane]-(others[0] if others else 0)>=7:filtered.append(lane)
        else:filtered.append(lane)
    if not filtered:filtered=[hr[0]["lane"]]
    cand=[]
    for a in filtered:
        for b in [r["lane"] for r in sr[:5]]:
            if b==a:continue
            for c in [r["lane"] for r in tr[:5]]:
                if c in (a,b):continue
                val=hm[a]*1.28+sm[b]*.78+tm[c]*.52
                if a==1 and b==2:
                    val+=4
                    if rm[2].get("class") in ("A1","A2"):val+=3
                cand.append((val,f"{a}-{b}-{c}"))
    cand.sort(reverse=True);out=[];seen=set()
    for _,bet in cand:
        if bet not in seen:out.append(bet);seen.add(bet)
        if len(out)>=max_bets:break
    if hr and hr[0]["lane"]==1 and 2 in rm:
        pos2=next((i for i,r in enumerate(hr,1) if r["lane"]==2),99)
        if rm[2].get("class") in ("A1","A2") or pos2<=3:
            cands=[r["lane"] for r in tr if r["lane"] not in (1,2)]
            if cands:
                safety=f"1-2-{cands[0]}"
                if safety not in out:
                    if len(out)>=max_bets:out[-1]=safety
                    else:out.append(safety)
    return out[:max_bets]


# ==============================================================================
# EV(期待値)判定機能
#
# v1.5.3で作成し、v1.7.1にも同じ考え方で組み込む。score_racers()が計算した
# head_score(1着適性スコア)を確率に変換し、Harville法で3連単の確率を推定、
# リアルタイムの3連単オッズと突き合わせて「モデル確率 × オッズ - 1」が
# プラスに大きい買い目だけを候補として示す。
#
# 重要: これは別モデルではなく、本体score_racersが出したhead_scoreを
# そのまま確率化して使っている。表示されている予想順位とEV判定が
# 同じ計算に基づくようにするため。
# ==============================================================================
import itertools

MIN_ODDS_COVERAGE=100  # 120通り中これ未満しか取れなければオッズ不完全とみなし候補を出さない
DEFAULT_EV_THRESHOLD=5.0  # 後方互換用(旧ev_pct基準)。新方式ではEV_MINを使う。

# --- v1.8 新確率モデルのパラメータ ---
# head_score/second_score/third_scoreは0-100スケールのスコアなので、
# Softmaxの温度もそのスケール感に合わせた値を初期値としている。
# 過去結果に合わせて最適化した値ではなく、検証ログが溜まってから
# 調整する前提の暫定値。
HEAD_TEMP=12.0
SECOND_TEMP=15.0
THIRD_TEMP=15.0

# EV_MIN: モデルEV(推定確率×オッズ)がこの倍率以上の時だけEV候補とする。
# 1.05 = モデルEVが+5%以上。検証用なので極端に高くしていない。
EV_MIN=1.05

EV_LOG_FILE=os.path.join(VERIFY_DIR,"ev_verify_log.jsonl")


def parse_odds3t(src):
    """3連単オッズページから {"a-b-c": オッズ} を抽出する。

    BOAT RACE公式の3連単表は、1着1〜6号艇の6ブロックが横並びで、
    各2着候補ごとに4行ずつ3着候補が並ぶ特殊な rowspan 表になっている。
    そのため画面上の文字列には「1-2-3」の組番が120通り明示されない。

    v1.9.1:
      1) 公式表の rowspan 構造を行単位で復元する matrix parser を最優先
      2) 従来の「1-2-3」表記 parser をフォールバックとして併用
    とし、取得件数が多い方を採用する。
    """

    def valid_boat(v):
        return v is not None and 1 <= v <= 6

    def parse_matrix_rows(html_src):
        """公式PC版の6列並列・rowspan表を復元する。

        1つの2着候補は4行で構成される。
          先頭行: [2着, 3着, オッズ] × 6ブロック = 18セル
          続く3行: [3着, オッズ] × 6ブロック = 12セル
        これが5ブロック(計20行)続くので120通りになる。
        """
        rows=[]
        for row_html in extract_rows(html_src or ""):
            cells=extract_cells(row_html)
            # 公式表以外の行を除外。rowspanの影響で基本18セル/12セル。
            if len(cells) not in (12,18):
                continue
            rows.append(cells)

        best={}
        # ページ内に他の12/18セル表が混じっても、20行窓で整合性を判定。
        for st in range(0, max(0, len(rows)-19)):
            window=rows[st:st+20]
            if len(window)<20:
                continue
            candidate={}
            second_state={a:None for a in range(1,7)}
            ok=True

            for r_idx,cells in enumerate(window):
                first_of_group=(r_idx % 4 == 0)
                expected=18 if first_of_group else 12
                if len(cells)!=expected:
                    ok=False;break

                chunk=3 if first_of_group else 2
                for col,a in enumerate(range(1,7)):
                    part=cells[col*chunk:(col+1)*chunk]
                    if first_of_group:
                        b=to_int(part[0],None)
                        c=to_int(part[1],None)
                        odd=to_float(part[2],None)
                        if not (valid_boat(b) and valid_boat(c)):
                            ok=False;break
                        second_state[a]=b
                    else:
                        b=second_state[a]
                        c=to_int(part[0],None)
                        odd=to_float(part[1],None)
                        if not (valid_boat(b) and valid_boat(c)):
                            ok=False;break

                    # 同一艇重複は3連単として不正。
                    if a==b or a==c or b==c:
                        ok=False;break

                    # 発売前/欠損は '-' 等になるので、その組だけ未登録にする。
                    if odd is not None and odd >= 1.0:
                        candidate[f"{a}-{b}-{c}"]=odd
                if not ok:
                    break

            # 120通り全部の組番構造が一意に生成できる窓だけ有力候補。
            if ok and len(candidate)>len(best):
                best=candidate
                if len(best)>=120:
                    break
        return best

    def parse_explicit_combos(html_src):
        """旧形式/別HTML向けフォールバック。明示的な1-2-3表記を拾う。"""
        odds={}
        combo_re=re.compile(r"\b([1-6])\s*[-－]\s*([1-6])\s*[-－]\s*([1-6])\b")
        odds_re=re.compile(r"\b(\d{1,5}(?:\.\d)?)\b")

        for row_html in extract_rows(html_src or ""):
            text=normalize_space(strip_tags(row_html))
            matches=list(combo_re.finditer(text))
            if not matches:
                continue
            for i,m in enumerate(matches):
                a,b,c=m.groups()
                if a==b or b==c or a==c:
                    continue
                start=m.end()
                end=matches[i+1].start() if i+1<len(matches) else len(text)
                segment=text[start:end]
                for om in odds_re.findall(segment):
                    v=to_float(om,None)
                    if v is not None and v>=1.0:
                        odds[f"{a}-{b}-{c}"]=v
                        break
        return odds

    matrix_odds=parse_matrix_rows(src)
    explicit_odds=parse_explicit_combos(src)

    # 公式PC版ではmatrix_oddsが通常120件。HTML変更時は取得数の多い方を採用。
    return matrix_odds if len(matrix_odds)>=len(explicit_odds) else explicit_odds


# 3連単オッズ専用: 通常OTAのiPhone UAとは分離する。
# BOAT RACE公式のPCオッズ表はアクセス条件によって返却HTMLが変わることがあるため、
# EV側だけデスクトップChrome相当UAで取得し、取得件数が不足した場合は複数条件で再取得する。
ODDS_DESKTOP_HEADERS={
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept-Language":"ja-JP,ja;q=0.9,en-US;q=0.7,en;q=0.6",
    "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Cache-Control":"no-cache",
    "Pragma":"no-cache",
    "Connection":"close",
}

def http_get_with_headers(url,headers):
    last=None
    for attempt in range(RETRY+1):
        try:
            req=Request(url,headers=headers)
            with urlopen(req,timeout=TIMEOUT) as res:
                raw=res.read()
                for enc in ("utf-8","cp932","shift_jis"):
                    try:return raw.decode(enc)
                    except:pass
                return raw.decode("utf-8",errors="replace")
        except Exception as e:
            last=e
            if attempt<RETRY:time.sleep(.8)
    raise RuntimeError(f"通信失敗: {last}")

def fetch_odds3t(jcd,hd,rno):
    url=make_url("odds3t",hd,jcd,rno)
    attempts=[]

    # 1) PC版オッズ表をデスクトップUAで取得。通常OTA本体のHEADERSは変更しない。
    try:
        h=dict(ODDS_DESKTOP_HEADERS)
        h["Referer"]=make_url("raceindex",hd,jcd,rno)
        src=http_get_with_headers(url,h)
        odds=parse_odds3t(src)
        attempts.append((len(odds),odds))
        if len(odds)>=MIN_ODDS_COVERAGE:
            return odds,url
    except Exception:
        pass

    # 2) キャッシュ回避用クエリを付けて再取得。
    try:
        sep="&" if "?" in url else "?"
        live_url=f"{url}{sep}_ota_ts={int(time.time()*1000)}"
        h=dict(ODDS_DESKTOP_HEADERS)
        h["Referer"]=url
        src=http_get_with_headers(live_url,h)
        odds=parse_odds3t(src)
        attempts.append((len(odds),odds))
        if len(odds)>=MIN_ODDS_COVERAGE:
            return odds,url
    except Exception:
        pass

    # 3) 旧iPhone UAでも試し、最も多く取得できた結果だけを採用。
    try:
        src=http_get(url)
        odds=parse_odds3t(src)
        attempts.append((len(odds),odds))
    except Exception:
        pass

    if attempts:
        attempts.sort(key=lambda x:x[0],reverse=True)
        return attempts[0][1],url
    return {},url


def softmax_dict(scores, temperature):
    """{lane: score} -> {lane: 確率(合計1.0)}。オーバーフロー防止のため最大値を引いてから指数化する。"""
    if not scores:
        return {}
    max_s=max(scores.values())
    exps={k:math.exp((v-max_s)/temperature) for k,v in scores.items()}
    total=sum(exps.values())
    if total<=0:
        n=len(scores)
        return {k:1.0/n for k in scores}
    return {k:v/total for k,v in exps.items()}


def estimated_trifecta_probs(ranked):
    """OTA本体のhead_score(1着適性)・second_score(2着適性)・third_score(3着適性)を
    使って3連単120通りの「推定確率」を作る。

    P(A-B-C) = P(Aが1着) × P(Bが2着|A除外) × P(Cが3着|A,B除外)
    という条件付き確率の形にしており、それぞれの段階で残っている艇だけを
    対象に個別にSoftmax変換(再正規化)している。これにより120通りの合計は
    数学的に必ず1.0になる(浮動小数点誤差を除く)。

    これはOTA本体のスコアリング(head_score等)を一切変更せず、EV機能側だけで
    それらのスコアの「使い方」を変えているだけであることに注意。
    """
    head_scores={r["lane"]:r.get("head_score",0.0) for r in ranked}
    second_scores={r["lane"]:r.get("second_score",0.0) for r in ranked}
    third_scores={r["lane"]:r.get("third_score",0.0) for r in ranked}
    lanes=list(head_scores.keys())

    p1=softmax_dict(head_scores,HEAD_TEMP)

    probs={}
    debug={}
    for a in lanes:
        remaining2={k:v for k,v in second_scores.items() if k!=a}
        p2=softmax_dict(remaining2,SECOND_TEMP)
        for b in lanes:
            if b==a:continue
            remaining3={k:v for k,v in third_scores.items() if k not in (a,b)}
            p3=softmax_dict(remaining3,THIRD_TEMP)
            for c in lanes:
                if c in (a,b):continue
                combo=f"{a}-{b}-{c}"
                prob=p1[a]*p2[b]*p3[c]
                probs[combo]=prob
                debug[combo]={
                    "head_score_a":head_scores[a],"p1_a":p1[a],
                    "second_score_b":second_scores[b],"p2_b_given_a":p2[b],
                    "third_score_c":third_scores[c],"p3_c_given_ab":p3[c],
                }
    return probs,debug


def compute_ev_table(probs,debug,odds,ev_min,bets):
    """probs: {combo: 推定確率}, debug: {combo: 内訳}, odds: {combo: オッズ}
    bets: 既存OTAの上位7点(順序付き)。EV候補が既存7点の何位に相当するかを
    付記するためだけに使う(比較表示用で、選定ロジックには影響しない)。"""
    bets_index={b:i+1 for i,b in enumerate(bets)}  # 1始まりの順位
    rows=[]
    for combo,p in probs.items():
        o=odds.get(combo)
        if o is None:continue
        model_ev=p*o  # 倍率(1.05 = +5%)
        d=debug.get(combo,{})
        rows.append({
            "combo":combo,
            "estimated_prob_pct":round(p*100.0,4),
            "odds":o,
            "model_ev":round(model_ev,4),
            "model_ev_pct":round((model_ev-1.0)*100.0,2),
            "ota_rank":bets_index.get(combo),  # Noneなら既存7点に含まれない
            "debug":{
                "head_score_a":round(d.get("head_score_a",0.0),2),
                "p1_a":round(d.get("p1_a",0.0),6),
                "second_score_b":round(d.get("second_score_b",0.0),2),
                "p2_b_given_a":round(d.get("p2_b_given_a",0.0),6),
                "third_score_c":round(d.get("third_score_c",0.0),2),
                "p3_c_given_ab":round(d.get("p3_c_given_ab",0.0),6),
            },
        })
    rows.sort(key=lambda r:r["model_ev"],reverse=True)

    if len(odds)<MIN_ODDS_COVERAGE:
        candidates=[]
    else:
        candidates=[r for r in rows if r["model_ev"]>=ev_min]
    return rows,candidates


def ev_log_write(record):
    """EV検証用ログをJSONLで1行追記する。書き込みに失敗しても例外を
    投げない(ログはあくまで検証用の副産物であり、本体機能を止めない)。"""
    try:
        with open(EV_LOG_FILE,"a",encoding=ENC_NAME) as f:
            f.write(json.dumps(record,ensure_ascii=False)+"\n")
    except Exception as e:
        print("EVログ書き込みエラー:",e)


def compute_ev(ranked,bets,confidence,before,jcd,hd,rno,ev_min=EV_MIN):
    """OTA本体のhead_score/second_score/third_scoreを使い、着順ごとに
    独立したSoftmaxで推定確率を作り、実オッズと突き合わせてEV判定する。
    オッズ取得に失敗しても例外を投げず、理由を付けて空の結果を返す
    (EVはあくまで追加情報であり、本体の予想表示自体は止めたくないため)。"""
    try:
        odds,odds_url=fetch_odds3t(jcd,hd,rno)
    except Exception as e:
        odds,odds_url=None,None
        ev_result={"available":False,"reason":f"オッズ取得に失敗しました: {e}",
                   "odds_count":0,"ranking":[],"candidates":[],"ev_top2":[]}
        ev_log_write({
            "logged_at":datetime.datetime.now().isoformat(timespec="seconds"),
            "date":hd,"venue_code":jcd,"race":int(rno),
            "confidence":confidence,
            "wind_speed":before.get("wind_speed"),"wave_height":before.get("wave_height"),
            "stable_board":before.get("stable_board"),
            "ota_bets":bets,
            "ev_available":False,"ev_error":str(e),
            "ev_candidates":[],
        })
        return ev_result

    probs,debug=estimated_trifecta_probs(ranked)

    # 合計が1.0付近になっているかの自己チェック(バグ検知用)。
    # ここが大きくズレる場合はconsole/ログで検知できるようにしておく。
    prob_sum=sum(probs.values())
    if abs(prob_sum-1.0)>0.01:
        print(f"[EV警告] 推定確率の合計が1.0から乖離しています: {prob_sum}")

    ranking,candidates=compute_ev_table(probs,debug,odds,ev_min,bets)

    reason=None
    if len(odds)<MIN_ODDS_COVERAGE:
        reason=f"オッズ取得件数が{len(odds)}/120と不十分なため、EV候補は非表示にしています(数字自体は信用できません)。"

    # 異常なモデルEVが大量発生していないかの簡易チェック(確率モデル破綻の検知用)
    extreme=[r for r in ranking if r["model_ev_pct"]>=300]
    if len(extreme)>=5:
        print(f"[EV警告] モデルEV+300%以上が{len(extreme)}件。確率モデルの異常の可能性があります。")

    ev_top2=candidates[:2]

    ev_result={"available":True,"reason":reason,"odds_count":len(odds),"odds_url":odds_url,
               "ev_min":ev_min,"ranking":ranking[:15],"candidates":candidates,"ev_top2":ev_top2,
               "prob_sum_check":round(prob_sum,6),
               "note":"推定確率は統計的に校正済みの真の的中確率ではありません。モデルEVも参考値です。"}

    ev_log_write({
        "logged_at":datetime.datetime.now().isoformat(timespec="seconds"),
        "date":hd,"venue_code":jcd,"race":int(rno),
        "confidence":confidence,
        "wind_speed":before.get("wind_speed"),"wave_height":before.get("wave_height"),
        "stable_board":before.get("stable_board"),
        "ota_bets":bets,
        "racers_scores":[{"lane":r["lane"],"head_score":r.get("head_score"),
                           "second_score":r.get("second_score"),"third_score":r.get("third_score")}
                          for r in sorted(ranked,key=lambda x:x["lane"])],
        "odds_count":len(odds),"prob_sum_check":round(prob_sum,6),
        "ev_available":True,
        "ev_candidates":[{
            "rank":i+1,"combo":r["combo"],"estimated_prob_pct":r["estimated_prob_pct"],
            "odds":r["odds"],"model_ev":r["model_ev"],"model_ev_pct":r["model_ev_pct"],
            "ota_rank":r["ota_rank"],
        } for i,r in enumerate(ev_top2)],
        # 結果はまだ分からないのでnullで確保しておき、後から/api/verify/updateに
        # 相当する仕組みで埋める前提の枠だけ用意しておく。
        "result":None,"hit_ev_rank":None,"payout_100":None,
    })

    return ev_result


def analyze(jcd,rno,hd,ev_min=EV_MIN,include_ev=False):
    # 通常版とEV版はキャッシュを分離。通常版ではオッズ取得/EV計算をしない。
    mode="ev" if include_ev else "normal"
    key=f"{hd}:{jcd}:{rno}:{mode}:{ev_min if include_ev else '-'}";now=time.time();cached=CACHE.get(key)
    if cached and now-cached["time"]<=CACHE_SECONDS:
        data=dict(cached["data"]);data["cached"]=True;return data
    racers=parse_racelist(http_get(make_url("racelist",hd,jcd,rno)))
    before=parse_beforeinfo(http_get(make_url("beforeinfo",hd,jcd,rno)))
    racers=merge_data(racers,before)
    if len(racers)<3:raise RuntimeError("出走表を取得できません。未開催・公開前・HTML変更の可能性があります。")
    ranked=score_racers(racers,before);judge,confidence,notes=race_judgement(ranked,before);bets=generate_bets(ranked,7)
    result={"app_version":APP_VERSION,"venue_code":jcd,"venue":VENUES[jcd],"race":int(rno),"date":hd,
            "judge":judge,"confidence":confidence,"coverage":round(data_coverage(ranked,before)*100,1),
            "notes":notes,"bets":bets,"weather":before,"racers":sorted(ranked,key=lambda x:x["lane"]),
            "overall_rank":[r["lane"] for r in sorted(ranked,key=lambda x:x["overall_score"],reverse=True)],
            "head_rank":[r["lane"] for r in sorted(ranked,key=lambda x:x["head_score"],reverse=True)],
            "second_rank":[r["lane"] for r in sorted(ranked,key=lambda x:x["second_score"],reverse=True)],
            "third_rank":[r["lane"] for r in sorted(ranked,key=lambda x:x["third_score"],reverse=True)],
            "cached":False}
    if include_ev:
        result["ev"]=compute_ev(ranked,bets,confidence,before,jcd,hd,rno,ev_min=ev_min)
    CACHE[key]={"time":now,"data":result};return result

def supabase_enabled():
    return bool(SUPABASE_URL and SUPABASE_KEY)

def load_logs():
    if supabase_enabled():
        try:
            all_logs=[]
            offset=0

            while True:
                url=(
                    f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
                    f"?select=payload&order=sort_key.asc"
                    f"&limit={SUPABASE_PAGE_SIZE}&offset={offset}"
                )
                req=Request(url,headers={
                    "apikey":SUPABASE_KEY,
                    "Authorization":f"Bearer {SUPABASE_KEY}",
                    "Accept":"application/json"
                })
                with urlopen(req,timeout=TIMEOUT) as res:
                    rows=json.loads(res.read().decode("utf-8"))

                if not isinstance(rows,list):
                    break

                for row in rows:
                    payload=row.get("payload") if isinstance(row,dict) else None
                    if isinstance(payload,dict):
                        all_logs.append(payload)

                if len(rows)<SUPABASE_PAGE_SIZE:
                    break

                offset+=SUPABASE_PAGE_SIZE

            return all_logs

        except Exception as e:
            print("Supabase load error:",e)

    try:
        with open(VERIFY_FILE,"r",encoding=ENC_NAME) as f:
            d=json.load(f)
            return d if isinstance(d,list) else []
    except Exception:
        return []

def save_logs(logs):
    if supabase_enabled():
        try:
            rows=[]
            now=datetime.datetime.now(datetime.timezone.utc).isoformat()
            for rec in logs:
                key=rec.get("key")
                if not key:
                    continue
                rows.append({
                    "race_key":key,
                    "sort_key":f"{rec.get('date','')}:{rec.get('venue_code','')}:{int(rec.get('race',0)):02d}",
                    "payload":rec,
                    "updated_at":now
                })

            if rows:
                url=f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?on_conflict=race_key"
                data=json.dumps(rows,ensure_ascii=False).encode("utf-8")
                req=Request(
                    url,
                    data=data,
                    method="POST",
                    headers={
                        "apikey":SUPABASE_KEY,
                        "Authorization":f"Bearer {SUPABASE_KEY}",
                        "Content-Type":"application/json",
                        "Prefer":"resolution=merge-duplicates,return=minimal"
                    }
                )
                with urlopen(req,timeout=TIMEOUT) as res:
                    res.read()
            return
        except Exception as e:
            print("Supabase save error:",e)

    tmp=VERIFY_FILE+".tmp"
    with open(tmp,"w",encoding=ENC_NAME) as f:
        json.dump(logs,f,ensure_ascii=False,indent=2)
    os.replace(tmp,VERIFY_FILE)

def save_analysis_log(data):
    logs=load_logs()
    key=f"{data['date']}:{str(data['venue_code']).zfill(2)}:{int(data['race'])}"
    old=next((x for x in logs if x.get("key")==key),None)
    rec={
        "key":key,"saved_at":datetime.datetime.now().isoformat(timespec="seconds"),
        "app_version":data.get("app_version",""),"date":data["date"],
        "venue_code":str(data["venue_code"]).zfill(2),"venue":data.get("venue",""),
        "race":int(data["race"]),"judge":data.get("judge",""),
        "confidence":int(data.get("confidence",0) or 0),"coverage":float(data.get("coverage",0) or 0),
        "bets":list(data.get("bets") or [])[:7],"result":None,"payout_100":None,
        "result_status":"pending","hit_rank":None}
    ev=data.get("ev")
    if isinstance(ev,dict):
        rec["ev_saved_at"]=datetime.datetime.now().isoformat(timespec="seconds")
        rec["ev_model_version"]=APP_VERSION;rec["ev_min"]=ev.get("ev_min")
        rec["ev_odds_count"]=ev.get("odds_count",0);rec["ev_prob_sum_check"]=ev.get("prob_sum_check")
        rec["ev_candidates"]=list(ev.get("ev_top2") or [])[:2];rec["ev_available"]=bool(ev.get("available"))
        rec["ev_reason"]=ev.get("reason")
    if old:
        for k in ("result","payout_100","result_status","hit_rank","result_checked_at","hit_ev_rank"):
            if old.get(k) is not None:rec[k]=old.get(k)
        if "ev" not in data:
            for k in ("ev_saved_at","ev_model_version","ev_min","ev_odds_count","ev_prob_sum_check","ev_candidates","ev_available","ev_reason","hit_ev_rank"):
                if k in old:rec[k]=old.get(k)
    if rec.get("result") and rec.get("ev_candidates"):
        try:
            combos=[x.get("combo") for x in rec.get("ev_candidates",[])]
            rec["hit_ev_rank"]=combos.index(rec["result"])+1 if rec["result"] in combos else None
        except Exception:rec["hit_ev_rank"]=None
    if supabase_enabled():save_logs([rec])
    else:
        logs=[rec if x.get("key")==key else x for x in logs] if old else logs+[rec]
        logs.sort(key=lambda x:(x.get("date",""),x.get("venue_code",""),int(x.get("race",0))))
        save_logs(logs)

def raceresult_url(hd,jcd,rno):
    return "https://www.boatrace.jp/owpc/pc/race/raceresult?"+urlencode({
        "hd":hd,
        "jcd":str(jcd).zfill(2),
        "rno":int(rno)
    })

def parse_result_page(src):
    if not src:
        return None

    text = strip_tags(src)
    cleaned = (
        text.replace("−","-")
            .replace("‐","-")
            .replace("ー","-")
            .replace("–","-")
            .replace("—","-")
    )

    pos = cleaned.find("3連単")
    if pos < 0:
        return None

    tail = cleaned[pos:pos+500]

    m = re.search(
        r"3連単\s*([1-6])\s*-\s*([1-6])\s*-\s*([1-6])"
        r"\s*[¥￥]\s*([\d,]+)",
        tail
    )

    if not m:
        m = re.search(
            r"3連単.*?([1-6])\s*-\s*([1-6])\s*-\s*([1-6])"
            r".*?[¥￥]\s*([\d,]+)",
            tail,
            re.S
        )

    if not m:
        return None

    return {
        "result": f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
        "payout_100": int(m.group(4).replace(",",""))
    }

def update_results(limit=VERIFY_UPDATE_LIMIT):
    logs=load_logs()
    checked=0
    updated=0
    errors=[]

    pending=[r for r in logs if r.get("result_status")!="done"]

    pending.sort(key=lambda r:(
        r.get("result_checked_at") or "",
        r.get("date",""),
        r.get("venue_code",""),
        int(r.get("race",0) or 0)
    ))

    targets=pending[:max(1,int(limit))]
    touched=[]

    for rec in targets:
        checked+=1
        try:
            url=raceresult_url(
                rec["date"],
                rec["venue_code"],
                rec["race"]
            )

            src=http_get(url)
            p=parse_result_page(src)

            rec["result_checked_at"]=datetime.datetime.now().isoformat(timespec="seconds")

            if p:
                rec["result"]=p["result"]
                rec["payout_100"]=p["payout_100"]
                rec["result_status"]="done"

                try:
                    rec["hit_rank"]=(rec.get("bets") or []).index(rec["result"])+1
                except ValueError:
                    rec["hit_rank"]=None
                try:
                    ev_combos=[x.get("combo") for x in (rec.get("ev_candidates") or [])]
                    rec["hit_ev_rank"]=ev_combos.index(rec["result"])+1 if rec["result"] in ev_combos else None
                except Exception:
                    rec["hit_ev_rank"]=None
                updated+=1
            else:
                rec["result_status"]="pending"

            touched.append(rec)

        except Exception as e:
            rec["result_checked_at"]=datetime.datetime.now().isoformat(timespec="seconds")
            touched.append(rec)
            errors.append({
                "key":rec.get("key"),
                "error":str(e)
            })

    if supabase_enabled():
        if touched:
            save_logs(touched)
    else:
        save_logs(logs)

    remaining=max(0,len(pending)-checked)
    return checked,updated,remaining,len(errors)

def calc_bucket(records,n,stake=200):
    done=[r for r in records if r.get("result_status")=="done" and r.get("result")]
    races=len(done);investment=races*n*stake;hits=payout=0
    for r in done:
        rank=r.get("hit_rank")
        if rank and rank<=n:
            hits+=1;payout+=int(round((r.get("payout_100") or 0)*(stake/100)))
    return {"races":races,"hits":hits,"hit_rate":round(hits/races*100,2) if races else 0,
            "investment":investment,"payout":payout,"profit":payout-investment,
            "return_rate":round(payout/investment*100,2) if investment else 0}

def stats(stake=200):
    logs=load_logs();done=[r for r in logs if r.get("result_status")=="done" and r.get("result")]
    buckets={str(n):calc_bucket(done,n,stake) for n in range(1,8)}
    venues=[]
    for code,name in VENUES.items():
        sub=[r for r in done if r.get("venue_code")==code]
        if sub:
            b=calc_bucket(sub,2,stake)
            venues.append({"venue":name,"races":len(sub),"top2_hit_rate":b["hit_rate"],"top2_return_rate":b["return_rate"],"top2_profit":b["profit"]})
    bands=[]
    for label,lo,hi in [("90-94",90,94),("80-89",80,89),("70-79",70,79),("60-69",60,69),("0-59",0,59)]:
        sub=[r for r in done if lo<=int(r.get("confidence",0) or 0)<=hi]
        if sub:
            b=calc_bucket(sub,2,stake);bands.append({"band":label,"races":len(sub),"hit_rate":b["hit_rate"],"return_rate":b["return_rate"],"profit":b["profit"]})
    return {"total_saved":len(logs),"completed":len(done),"pending":len(logs)-len(done),"buckets":buckets,"venues":venues,"confidence_bands":bands}


def calc_ev_bucket(records,stake=VERIFY_STAKE_YEN):
    done=[r for r in records if r.get("result_status")=="done" and r.get("result") and r.get("ev_candidates")]
    races=len(done);tickets=sum(len(r.get("ev_candidates") or []) for r in done);investment=tickets*stake;hits=0;payout=0
    for r in done:
        if r.get("hit_ev_rank"):
            hits+=1;payout+=int(round((r.get("payout_100") or 0)*(stake/100)))
    return {"races":races,"tickets":tickets,"hits":hits,"hit_rate":round(hits/races*100,2) if races else 0,
            "investment":investment,"payout":payout,"profit":payout-investment,
            "return_rate":round(payout/investment*100,2) if investment else 0}

def ev_stats(stake=VERIFY_STAKE_YEN):
    logs=load_logs();done=[r for r in logs if r.get("result_status")=="done" and r.get("result") and r.get("ev_candidates")]
    overall=calc_ev_bucket(done,stake)
    tickets=[]
    for r in done:
        for c in (r.get("ev_candidates") or []):
            mev=to_float(c.get("model_ev"),None)
            if mev is not None:tickets.append({"model_ev":mev,"hit":c.get("combo")==r.get("result"),"payout_100":r.get("payout_100") or 0})
    bands=[]
    for label,lo,hi in [("105-120%",1.05,1.20),("120-150%",1.20,1.50),("150-200%",1.50,2.00),("200%以上",2.00,None)]:
        sub=[x for x in tickets if x["model_ev"]>=lo and (hi is None or x["model_ev"]<hi)];inv=len(sub)*stake
        hit=sum(1 for x in sub if x["hit"]);pay=sum(int(round(x["payout_100"]*(stake/100))) for x in sub if x["hit"])
        bands.append({"band":label,"tickets":len(sub),"hits":hit,"hit_rate":round(hit/len(sub)*100,2) if sub else 0,
                      "investment":inv,"payout":pay,"profit":pay-inv,"return_rate":round(pay/inv*100,2) if inv else 0})
    venues=[]
    for code,name in VENUES.items():
        sub=[r for r in done if r.get("venue_code")==code]
        if sub:
            b=calc_ev_bucket(sub,stake);b.update({"venue_code":code,"venue":name});venues.append(b)
    venues.sort(key=lambda x:(x["return_rate"],x["races"]),reverse=True)
    return {"saved_ev_races":sum(1 for r in logs if r.get("ev_candidates")),"completed_ev_races":len(done),
            "pending_ev_races":sum(1 for r in logs if r.get("ev_candidates") and r.get("result_status")!="done"),
            "stake_per_ticket":stake,"overall":overall,"bands":bands,"venues":venues}

@app.route("/")
def index():
    today=datetime.datetime.now().strftime("%Y%m%d")
    return render_template("index.html",venues=VENUES,today=today,version=APP_VERSION)

@app.route("/api/analyze")
def api_analyze():
    try:
        jcd=str(request.args.get("jcd","18")).zfill(2);rno=int(request.args.get("rno","1"));hd=request.args.get("hd") or datetime.datetime.now().strftime("%Y%m%d")
        if jcd not in VENUES:return jsonify({"ok":False,"error":"場コードが不正です"}),400
        if not 1<=rno<=12:return jsonify({"ok":False,"error":"レース番号は1～12です"}),400
        data=analyze(jcd,rno,hd,include_ev=False);save_analysis_log(data);return jsonify({"ok":True,"data":data})
    except Exception as e:return jsonify({"ok":False,"error":str(e)}),500

@app.route("/api/ev/analyze")
@ev_login_required
def api_ev_analyze():
    try:
        jcd=str(request.args.get("jcd","18")).zfill(2);rno=int(request.args.get("rno","1"));hd=request.args.get("hd") or datetime.datetime.now().strftime("%Y%m%d");ev_min=to_float(request.args.get("ev_min"),EV_MIN)
        if jcd not in VENUES:return jsonify({"ok":False,"error":"場コードが不正です"}),400
        if not 1<=rno<=12:return jsonify({"ok":False,"error":"レース番号は1～12です"}),400
        data=analyze(jcd,rno,hd,ev_min=ev_min,include_ev=True);save_analysis_log(data);return jsonify({"ok":True,"data":data})
    except Exception as e:return jsonify({"ok":False,"error":str(e)}),500

@app.route("/api/ev/stats")
@ev_login_required
def api_ev_stats():return jsonify({"ok":True,"stats":ev_stats(VERIFY_STAKE_YEN)})

@app.route("/api/ev/update",methods=["GET","POST"])
@ev_login_required
def api_ev_update():
    c,u,remaining,error_count=update_results(VERIFY_UPDATE_LIMIT)
    return jsonify({"ok":True,"checked":c,"updated":u,"remaining":remaining,"errors":error_count,"limit":VERIFY_UPDATE_LIMIT})

@app.route("/api/verify/update",methods=["GET","POST"])
def verify_update():
    c,u,remaining,error_count=update_results(VERIFY_UPDATE_LIMIT)
    return jsonify({
        "ok":True,
        "checked":c,
        "updated":u,
        "remaining":remaining,
        "errors":error_count,
        "limit":VERIFY_UPDATE_LIMIT
    })

@app.route("/api/verify/stats")
def verify_stats():
    return jsonify({"ok":True,"stats":stats(200)})

@app.route("/api/verify/storage")
def verify_storage():
    return jsonify({
        "ok":True,
        "mode":"supabase" if supabase_enabled() else "local",
        "persistent":bool(supabase_enabled()),
        "table":SUPABASE_TABLE if supabase_enabled() else None
    })


@app.route("/ev/login",methods=["GET","POST"])
def ev_login():
    if ev_auth_ok():return redirect(url_for("ev_page"))
    error=""
    if request.method=="POST":
        if not EV_PASSWORD:error="RenderのEnvironmentに EV_PASSWORD が設定されていません。"
        elif hmac.compare_digest(str(request.form.get("password","")),str(EV_PASSWORD)):
            session.clear();session["ev_auth"]=True;session.permanent=True
            nxt=request.form.get("next") or url_for("ev_page")
            if not str(nxt).startswith("/"):nxt=url_for("ev_page")
            return redirect(nxt)
        else:error="パスワードが違います。"
    nxt=request.args.get("next") or url_for("ev_page")
    err_html=("<p class='e'>"+html.escape(error)+"</p>") if error else ""
    page="""<!doctype html><html lang='ja'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>OTA EV Login</title><style>body{font-family:-apple-system,sans-serif;background:#0d1420;color:#fff;margin:0}.w{max-width:420px;margin:70px auto;padding:20px}.c{background:#182233;border-radius:18px;padding:22px}input,button{box-sizing:border-box;width:100%;padding:14px;border-radius:12px;font-size:17px}input{border:1px solid #526174;background:#fff;color:#111;margin:12px 0}button{border:0;background:#2f7df6;color:#fff;font-weight:700}.e{color:#ff9b9b}.s{color:#aeb9c8;font-size:13px;line-height:1.5}</style></head><body><div class='w'><div class='c'><h2>🔐 OTA EV 専用</h2><p class='s'>一度ログインすると、このiPhoneでは最大__DAYS__日間ログイン状態を保持します。</p>__ERR__<form method='post'><input type='hidden' name='next' value='__NEXT__'><input name='password' type='password' autocomplete='current-password' placeholder='EV専用パスワード' required><button type='submit'>EVモードを開く</button></form></div></div></body></html>"""
    page=page.replace("__DAYS__",str(EV_LOGIN_DAYS)).replace("__ERR__",err_html).replace("__NEXT__",html.escape(str(nxt)))
    return Response(page,mimetype="text/html")

@app.route("/ev/logout")
def ev_logout():session.clear();return redirect(url_for("ev_login"))

@app.route("/ev")
@ev_login_required
def ev_page():
    today=datetime.datetime.now().strftime("%Y%m%d")
    opts=''.join(f'<option value="{c}">{c} {html.escape(n)}</option>' for c,n in VENUES.items())
    races=''.join(f'<option value="{i}">{i}R</option>' for i in range(1,13))
    page="""<!doctype html><html lang='ja'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>OTA QUICK SNIPER EV</title><style>body{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;background:#0c111a;color:#edf3ff;margin:0}.w{max-width:940px;margin:auto;padding:14px}.c{background:#151e2c;border:1px solid #263247;border-radius:16px;padding:14px;margin:12px 0}h2,h3{margin:6px 0 12px}select,input,button{font-size:16px;border-radius:10px;padding:11px}select,input{background:#fff;color:#111;border:0}button{border:0;background:#2f7df6;color:#fff;font-weight:700}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.row>*{flex:1;min-width:110px}.muted{color:#aab7c9;font-size:13px}.bets{font-size:22px;font-weight:800;letter-spacing:.5px}.ev{font-size:18px;font-weight:800}.good{color:#6fe39c}.bad{color:#ff8a8a}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:8px;border-bottom:1px solid #2a3548;text-align:right}th:first-child,td:first-child{text-align:left}a{color:#8eb9ff}#msg{white-space:pre-wrap}@media(max-width:600px){.row>*{min-width:46%}.bets{font-size:20px}}</style></head><body><div class='w'><div class='row'><h2 style='flex:3'>🚤 OTA QUICK SNIPER <span style='color:#67d4ff'>EV専用</span></h2><a href='/ev/logout' style='flex:0;white-space:nowrap'>ログアウト</a></div><div class='c'><div class='row'><select id='jcd'>__OPTS__</select><input id='hd' value='__TODAY__' inputmode='numeric' maxlength='8'><select id='rno'>__RACES__</select><input id='evmin' value='__EVMIN__' inputmode='decimal'><button onclick='go()'>解析</button></div><p class='muted'>EV閾値は倍率。1.05=モデルEV +5%以上。通常OTAロジックは変更しません。</p><div id='msg'></div></div><div class='c'><h3>通常OTA</h3><div id='normal'>まだ解析していません。</div></div><div class='c'><h3>EV TOP2（検証中）</h3><div id='evbox'>まだ解析していません。</div><p class='muted'>※モデルEVは校正済みの真の期待値ではありません。実績との相関を検証するための値です。</p></div><div class='c'><div class='row'><h3 style='flex:3'>EV検証</h3><button onclick='upd()' style='flex:1'>公式結果を更新</button></div><div id='sum'>読み込み中...</div><h4>EV帯別</h4><table><thead><tr><th>モデルEV</th><th>券数</th><th>的中率</th><th>回収率</th><th>収支</th></tr></thead><tbody id='bands'></tbody></table><h4>会場別</h4><table><thead><tr><th>会場</th><th>R</th><th>的中率</th><th>回収率</th><th>収支</th></tr></thead><tbody id='venues'></tbody></table></div><script>
const yen=n=>new Intl.NumberFormat('ja-JP').format(n)+'円',pct=n=>Number(n||0).toFixed(1)+'%';
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
async function go(){msg.textContent='解析中...';normal.textContent='';evbox.textContent='';try{const q=new URLSearchParams({jcd:jcd.value,hd:hd.value,rno:rno.value,ev_min:evmin.value});const j=await (await fetch('/api/ev/analyze?'+q)).json();if(!j.ok)throw new Error(j.error||'解析失敗');const d=j.data;msg.textContent=`${d.venue} ${d.race}R / ${d.judge} / 信頼度 ${d.confidence}%`;normal.innerHTML=`<div class='bets'>${d.bets.map((b,i)=>`${i+1}位 ${esc(b)}`).join('　')}</div><p class='muted'>1着順位: ${d.head_rank.join('→')} / 2着順位: ${d.second_rank.join('→')} / 3着順位: ${d.third_rank.join('→')}</p>`;const e=d.ev||{};if(!e.available){evbox.innerHTML=`<span class='bad'>${esc(e.reason||'EV取得不可')}</span>`}else if(!(e.ev_top2||[]).length){evbox.innerHTML=`EV条件を満たす候補なし（オッズ ${e.odds_count}/120）`}else{evbox.innerHTML=e.ev_top2.map((x,i)=>`<div class='ev'>${i+1}位 ${esc(x.combo)}　推定確率 ${x.estimated_prob_pct}%　オッズ ${x.odds}倍　<span class='${x.model_ev>=1?'good':'bad'}'>モデルEV ${(x.model_ev*100).toFixed(1)}%</span>　${x.ota_rank?'OTA通常'+x.ota_rank+'位':'OTA通常7点外'}</div>`).join('<hr style="border-color:#2a3548">')}await loadStats()}catch(e){msg.textContent='エラー: '+e.message}}
async function loadStats(){const j=await (await fetch('/api/ev/stats')).json();if(!j.ok)return;const s=j.stats,o=s.overall;sum.innerHTML=`保存EVレース <b>${s.saved_ev_races}</b> / 結果確定 <b>${s.completed_ev_races}</b> / 待ち <b>${s.pending_ev_races}</b><br>EV TOP候補全体: ${o.races}R・${o.tickets}券 / 的中率 ${pct(o.hit_rate)} / 回収率 <b class='${o.return_rate>=100?'good':'bad'}'>${pct(o.return_rate)}</b> / 収支 ${yen(o.profit)}`;bands.innerHTML=s.bands.map(b=>`<tr><td>${b.band}</td><td>${b.tickets}</td><td>${pct(b.hit_rate)}</td><td class='${b.return_rate>=100?'good':'bad'}'>${pct(b.return_rate)}</td><td>${yen(b.profit)}</td></tr>`).join('');venues.innerHTML=s.venues.map(v=>`<tr><td>${v.venue}</td><td>${v.races}</td><td>${pct(v.hit_rate)}</td><td class='${v.return_rate>=100?'good':'bad'}'>${pct(v.return_rate)}</td><td>${yen(v.profit)}</td></tr>`).join('')}
async function upd(){msg.textContent='公式結果を最大20R更新中...';const j=await (await fetch('/api/ev/update',{method:'POST'})).json();msg.textContent=j.ok?`確認${j.checked}R / 新規確定${j.updated}R / 残り${j.remaining}R`:(j.error||'更新失敗');await loadStats()}
loadStats();</script></div></body></html>"""
    page=page.replace("__OPTS__",opts).replace("__TODAY__",today).replace("__RACES__",races).replace("__EVMIN__",f"{EV_MIN:.2f}")
    return Response(page,mimetype="text/html")

@app.route("/verify")
def verify_page():
    return Response("""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OTA QUICK SNIPER 検証</title><style>
body{font-family:-apple-system,sans-serif;background:#f4f7fb;margin:0;color:#18202a}.w{max-width:900px;margin:auto;padding:16px}
.c{background:#fff;border-radius:14px;padding:14px;margin:12px 0}button{border:0;border-radius:10px;padding:12px;background:#1268d6;color:#fff;font-size:16px}
table{width:100%;border-collapse:collapse}th,td{padding:8px;border-bottom:1px solid #ddd;text-align:right}th:first-child,td:first-child{text-align:left}
.g{color:green;font-weight:bold}.b{color:#d33;font-weight:bold}</style></head><body><div class="w">
<h2>🚤 OTA QUICK SNIPER 検証</h2><div class="c"><button id="ub" onclick="upd()">公式結果を20R更新</button><p id="s">解析すると自動保存されます。結果更新は1回最大20Rです。</p></div>
<div class="c"><b>保存:</b> <span id="sv">-</span>　<b>確定:</b> <span id="dn">-</span>　<b>待ち:</b> <span id="pd">-</span></div>
<div class="c"><h3>上位何点まで買った場合</h3><table><thead><tr><th>買い方</th><th>的中率</th><th>回収率</th><th>収支</th></tr></thead><tbody id="br"></tbody></table></div>
<div class="c"><h3>会場別（上位2点）</h3><table><thead><tr><th>会場</th><th>R</th><th>的中率</th><th>回収率</th><th>収支</th></tr></thead><tbody id="vr"></tbody></table></div>
<div class="c"><h3>信頼度別（上位2点）</h3><table><thead><tr><th>信頼度</th><th>R</th><th>的中率</th><th>回収率</th><th>収支</th></tr></thead><tbody id="cr"></tbody></table></div>
<script>
const y=n=>new Intl.NumberFormat('ja-JP').format(n)+'円',p=n=>Number(n).toFixed(1)+'%';
async function load(){let j=await (await fetch('/api/verify/stats')).json(),s=j.stats;
sv.textContent=s.total_saved;dn.textContent=s.completed;pd.textContent=s.pending;
br.innerHTML='';for(let n=1;n<=7;n++){let b=s.buckets[String(n)];br.innerHTML+=`<tr><td>上位${n}点</td><td>${p(b.hit_rate)}</td><td class="${b.return_rate>=100?'g':'b'}">${p(b.return_rate)}</td><td class="${b.profit>=0?'g':'b'}">${y(b.profit)}</td></tr>`}
vr.innerHTML='';for(const v of s.venues){vr.innerHTML+=`<tr><td>${v.venue}</td><td>${v.races}</td><td>${p(v.top2_hit_rate)}</td><td class="${v.top2_return_rate>=100?'g':'b'}">${p(v.top2_return_rate)}</td><td>${y(v.top2_profit)}</td></tr>`}
cr.innerHTML='';for(const b of s.confidence_bands){cr.innerHTML+=`<tr><td>${b.band}</td><td>${b.races}</td><td>${p(b.hit_rate)}</td><td class="${b.return_rate>=100?'g':'b'}">${p(b.return_rate)}</td><td>${y(b.profit)}</td></tr>`}}
async function upd(){ub.disabled=true;s.textContent='最大20Rを更新中...';try{let j=await (await fetch('/api/verify/update',{method:'POST'})).json();s.textContent=`確認${j.checked}R / 新規確定${j.updated}R / 更新対象残り${j.remaining}R`;await load()}catch(e){s.textContent='更新に失敗しました。少し待って再度押してください。'}finally{ub.disabled=false}}load();
</script></div></body></html>""",mimetype="text/html")

if __name__=="__main__":
    port=int(os.environ.get("PORT","5000"))
    app.run(host="0.0.0.0",port=port,debug=False)
