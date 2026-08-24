# coding: utf-8
ENC_NAME="utf-8"
ECL_NAME="M"
TARGET_BYTES=262

import re,os,json,time,html,datetime
from urllib.request import Request,urlopen
from urllib.parse import urlencode
from flask import Flask,render_template,request,jsonify,Response

APP_VERSION="1.6.0 VERIFY"
APP_NAME="OTA QUICK SNIPER"
BASE_URL="https://www.boatrace.jp/owpc/pc/race"
TIMEOUT=15
RETRY=2

app=Flask(__name__)

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
VERIFY_DIR=os.environ.get("OTA_DATA_DIR",os.path.join(os.path.dirname(__file__),"ota_verify_data"))
VERIFY_FILE=os.path.join(VERIFY_DIR,"analysis_log.json")
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

def analyze(jcd,rno,hd):
    key=f"{hd}:{jcd}:{rno}";now=time.time();cached=CACHE.get(key)
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
            "third_rank":[r["lane"] for r in sorted(ranked,key=lambda x:x["third_score"],reverse=True)],"cached":False}
    CACHE[key]={"time":now,"data":result};return result

def load_logs():
    try:
        with open(VERIFY_FILE,"r",encoding=ENC_NAME) as f:
            d=json.load(f);return d if isinstance(d,list) else []
    except:return []

def save_logs(logs):
    tmp=VERIFY_FILE+".tmp"
    with open(tmp,"w",encoding=ENC_NAME) as f:json.dump(logs,f,ensure_ascii=False,indent=2)
    os.replace(tmp,VERIFY_FILE)

def save_analysis_log(data):
    logs=load_logs();key=f"{data['date']}:{str(data['venue_code']).zfill(2)}:{int(data['race'])}"
    old=next((x for x in logs if x.get("key")==key),None)
    rec={"key":key,"saved_at":datetime.datetime.now().isoformat(timespec="seconds"),"app_version":data.get("app_version",""),
         "date":data["date"],"venue_code":str(data["venue_code"]).zfill(2),"venue":data.get("venue",""),"race":int(data["race"]),
         "judge":data.get("judge",""),"confidence":int(data.get("confidence",0) or 0),"coverage":float(data.get("coverage",0) or 0),
         "bets":list(data.get("bets") or [])[:7],"result":None,"payout_100":None,"result_status":"pending","hit_rank":None}
    if old and old.get("result"):
        for k in ("result","payout_100","result_status","hit_rank","result_checked_at"):rec[k]=old.get(k)
    logs=[rec if x.get("key")==key else x for x in logs] if old else logs+[rec]
    logs.sort(key=lambda x:(x.get("date",""),x.get("venue_code",""),int(x.get("race",0))));save_logs(logs)

def resultlist_url(hd,jcd):
    return "https://www.boatrace.jp/owpc/pc/race/resultlist?"+urlencode({"hd":hd,"jcd":str(jcd).zfill(2)})

def parse_result(src,rno):
    for row in extract_rows(src):
        txt=strip_tags(row)
        if not re.search(rf"(?<!\d){int(rno)}R(?!\d)",txt):continue
        cleaned=txt.replace("−","-").replace("‐","-").replace("ー","-")
        m=re.search(r"(?<!\d)([1-6])\s*-\s*([1-6])\s*-\s*([1-6]).*?[¥￥]\s*([\d,]+)",cleaned)
        if not m:m=re.search(r"(?<!\d)([1-6])\s+([1-6])\s+([1-6]).*?[¥￥]\s*([\d,]+)",cleaned)
        if m:return {"result":f"{m.group(1)}-{m.group(2)}-{m.group(3)}","payout_100":int(m.group(4).replace(",",""))}
    return None

def update_results():
    logs=load_logs();pages={};checked=updated=0
    for rec in logs:
        if rec.get("result_status")=="done":continue
        checked+=1
        key=f"{rec['date']}:{rec['venue_code']}"
        try:
            if key not in pages:pages[key]=http_get(resultlist_url(rec["date"],rec["venue_code"]))
            p=parse_result(pages[key],rec["race"])
            if not p:continue
            rec["result"]=p["result"];rec["payout_100"]=p["payout_100"];rec["result_status"]="done"
            rec["result_checked_at"]=datetime.datetime.now().isoformat(timespec="seconds")
            try:rec["hit_rank"]=(rec.get("bets") or []).index(rec["result"])+1
            except ValueError:rec["hit_rank"]=None
            updated+=1
        except:pass
    save_logs(logs);return checked,updated

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

@app.route("/")
def index():
    today=datetime.datetime.now().strftime("%Y%m%d")
    return render_template("index.html",venues=VENUES,today=today,version=APP_VERSION)

@app.route("/api/analyze")
def api_analyze():
    try:
        jcd=str(request.args.get("jcd","18")).zfill(2);rno=int(request.args.get("rno","1"))
        hd=request.args.get("hd") or datetime.datetime.now().strftime("%Y%m%d")
        if jcd not in VENUES:return jsonify({"ok":False,"error":"場コードが不正です"}),400
        if not 1<=rno<=12:return jsonify({"ok":False,"error":"レース番号は1～12です"}),400
        data=analyze(jcd,rno,hd);save_analysis_log(data)
        return jsonify({"ok":True,"data":data})
    except Exception as e:return jsonify({"ok":False,"error":str(e)}),500

@app.route("/api/verify/update",methods=["GET","POST"])
def verify_update():
    c,u=update_results();return jsonify({"ok":True,"checked":c,"updated":u})

@app.route("/api/verify/stats")
def verify_stats():
    return jsonify({"ok":True,"stats":stats(200)})

@app.route("/verify")
def verify_page():
    return Response("""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OTA QUICK SNIPER 検証</title><style>
body{font-family:-apple-system,sans-serif;background:#f4f7fb;margin:0;color:#18202a}.w{max-width:900px;margin:auto;padding:16px}
.c{background:#fff;border-radius:14px;padding:14px;margin:12px 0}button{border:0;border-radius:10px;padding:12px;background:#1268d6;color:#fff;font-size:16px}
table{width:100%;border-collapse:collapse}th,td{padding:8px;border-bottom:1px solid #ddd;text-align:right}th:first-child,td:first-child{text-align:left}
.g{color:green;font-weight:bold}.b{color:#d33;font-weight:bold}</style></head><body><div class="w">
<h2>🚤 OTA QUICK SNIPER 検証</h2><div class="c"><button onclick="upd()">公式結果を更新</button><p id="s">解析すると自動保存されます。</p></div>
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
async function upd(){s.textContent='更新中...';let j=await (await fetch('/api/verify/update',{method:'POST'})).json();s.textContent=`確認${j.checked}R / 新規確定${j.updated}R`;load()}load();
</script></div></body></html>""",mimetype="text/html")

if __name__=="__main__":
    port=int(os.environ.get("PORT","5000"))
    app.run(host="0.0.0.0",port=port,debug=False)
