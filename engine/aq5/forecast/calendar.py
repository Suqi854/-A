"""Prediction deadlines on an explicit exchange calendar, never guessed weekdays."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser

from .. import db
from ..config import CN_TZ, now

HORIZONS = ("1d", "3d", "5d", "20d", "30m", "60m")
SESSIONS = ((time(9, 30), time(11, 30)), (time(13), time(15)))
CALENDAR_SOURCE = "https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20251222_10802510.shtml"
CALENDAR_INDEX = "https://www.sse.com.cn/disclosure/dealinstruc/closed/"
HOLIDAYS = ('元旦','春节','清明节','劳动节','端午节','中秋节','国庆节')
MAX_DOCUMENT_BYTES = 1024 * 1024
_SEED_CLOSURES = (("01-01", "01-03"), ("02-15", "02-23"), ("04-04", "04-06"),
                  ("05-01", "05-05"), ("06-19", "06-21"), ("09-25", "09-27"), ("10-01", "10-07"))


def init():
    with db.tx() as conn:
        conn.executescript('''CREATE TABLE IF NOT EXISTS forecast_calendars(
            year INTEGER PRIMARY KEY, source TEXT NOT NULL, published_at TEXT,
            observed_at TEXT NOT NULL, days_json TEXT NOT NULL, hash TEXT NOT NULL,
            source_document_hash TEXT NOT NULL, closed_ranges_json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS forecast_calendar_refresh(
            year INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS forecast_calendar_versions(
            hash TEXT PRIMARY KEY, year INTEGER NOT NULL, source TEXT NOT NULL, published_at TEXT,
            observed_at TEXT NOT NULL, days_json TEXT NOT NULL, source_document_hash TEXT NOT NULL,
            closed_ranges_json TEXT NOT NULL);
        ''')


def _table(name):
    return bool(db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)))


def _trusted_source(url):
    parsed=urllib.parse.urlsplit(url)
    return (parsed.scheme=='https' and parsed.netloc=='www.sse.com.cn' and not parsed.query and not parsed.fragment
            and (parsed.path=='/disclosure/dealinstruc/closed/' or
                 re.fullmatch(r'/disclosure/(?:dealinstruc/closed|announcement/general)/c/c_\d{8}_\d+\.shtml',parsed.path) is not None))


def _build_days(year, closed_ranges):
    if type(year) is not int or not 2020<=year<=2100 or not isinstance(closed_ranges,dict) or set(closed_ranges)!=set(HOLIDAYS):
        raise ValueError('annual_calendar_requires_all_seven_holidays')
    closed=set()
    normalized={}
    for holiday in HOLIDAYS:
        intervals=closed_ranges[holiday]
        if not isinstance(intervals,(list,tuple)) or not 1<=len(intervals)<=3:
            raise ValueError('invalid_holiday_ranges')
        normalized[holiday]=[]
        for interval in intervals:
            if not isinstance(interval,(list,tuple)) or len(interval)!=2:
                raise ValueError('invalid_closed_range')
            try:
                first,last=(date.fromisoformat(v) for v in interval)
            except (TypeError,ValueError):
                raise ValueError('invalid_closed_date') from None
            if first.year!=year or last.year!=year or not 0<=(last-first).days<=15:
                raise ValueError('calendar_year_or_range_mismatch')
            normalized[holiday].append([first.isoformat(),last.isoformat()])
            day=first
            while day<=last:
                closed.add(day); day+=timedelta(days=1)
    result=[];day=date(year,1,1)
    while day.year==year:
        if day.weekday()<5 and day not in closed:result.append(day.isoformat())
        day+=timedelta(days=1)
    if not 220<=len(result)<=255 or not 10<=sum(d.weekday()<5 for d in closed)<=40:
        raise ValueError('implausible_annual_exchange_calendar')
    return result,normalized


def import_calendar(year,closed_ranges,source,source_document_hash,*,verified=False,published_at=None,observed_at=None):
    """Internal audited import, never an anonymous HTTP write endpoint.

    A caller doing manual import must independently verify the full official
    annual document and supply its SHA-256. Automatic refresh verifies/derives it.
    """
    if verified is not True or not _trusted_source(source) or not re.fullmatch(r'[0-9a-f]{64}',str(source_document_hash)):
        raise ValueError('unverified_or_untrusted_calendar')
    days,normalized=_build_days(year,closed_ranges)
    observed=stamp(observed_at or now())
    if observed is None:
        raise ValueError('invalid_observation_time')
    published=date.fromisoformat(published_at) if published_at else None
    if published and (published>observed.date() or published.year not in (year-1,year)):
        raise ValueError('invalid_publication_date')
    payload={'year':year,'source':source,'published_at':published_at,'days':days,
             'closed_ranges':normalized,'source_document_hash':source_document_hash}
    digest=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    init()
    with db.tx() as conn:
        previous=conn.execute('SELECT hash FROM forecast_calendars WHERE year=?',(year,)).fetchone()
        conn.execute('INSERT OR IGNORE INTO forecast_calendar_versions VALUES(?,?,?,?,?,?,?,?)',
                     (digest,year,source,published_at,iso(observed),json.dumps(days),source_document_hash,
                      json.dumps(normalized,ensure_ascii=False,sort_keys=True)))
        conn.execute('INSERT OR REPLACE INTO forecast_calendars VALUES(?,?,?,?,?,?,?,?)',
                     (year,source,published_at,iso(observed),json.dumps(days),digest,source_document_hash,
                      json.dumps(normalized,ensure_ascii=False,sort_keys=True)))
    return {'year':year,'status':'ready','days':len(days),'hash':digest,'changed':not previous or previous['hash']!=digest,
            'source':source,'published_at':published_at,'observed_at':iso(observed)}


def official_days(year: int = 2026) -> list[str]:
    """Persisted verified official calendars first; only 2026 has a bundled seed."""
    if _table('forecast_calendars'):
        row=db.q1('SELECT days_json FROM forecast_calendars WHERE year=?',(year,))
        if row:
            return json.loads(row['days_json'])
    if year != 2026:
        return []
    closures={holiday:[[f'2026-{start}',f'2026-{end}']] for holiday,(start,end) in zip(HOLIDAYS,_SEED_CLOSURES)}
    return _build_days(2026,closures)[0]


def calendar_window(year):
    """Known adjacent years enable a year-end 20-session horizon after publication."""
    return official_days(year)+official_days(year+1)


class _OfficialHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text=[];self.links=[];self._hidden=0;self._link=None
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self._hidden+=1
        if tag=='a':self._link=[dict(attrs).get('href',''),[]]
        if tag in ('p','tr','div','h1','h2','h3','br'):self.text.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style'):self._hidden=max(0,self._hidden-1)
        if tag=='a' and self._link:
            self.links.append((self._link[0],''.join(self._link[1])));self._link=None
        if tag in ('p','tr','div','h1','h2','h3'):self.text.append('\n')
    def handle_data(self,value):
        if not self._hidden:
            self.text.append(value)
            if self._link:self._link[1].append(value)


def parse_official_document(raw,year):
    """Accept a full annual seven-holiday SSE table/notice, not one-holiday notices."""
    if not isinstance(raw,bytes) or len(raw)>MAX_DOCUMENT_BYTES:
        raise ValueError('calendar_document_too_large_or_invalid')
    parser=_OfficialHTML();parser.feed(raw.decode('utf-8-sig'))
    text=' '.join(''.join(parser.text).split())
    title=re.search(r'(?<!\d)(\d{4})年(?:全年)?休市安排',text)
    if not title or int(title[1])!=year:
        raise ValueError('annual_calendar_not_published_for_year')
    # The first annual title determines the applicable year. Full closure ranges
    # must appear before any related notices, which can otherwise supply fragments.
    body=text[title.end():].split('相关公告',1)[0].split('相关链接',1)[0]
    labels='|'.join(HOLIDAYS)
    label_pattern=re.compile(r'(?P<label>(?:'+labels+r')(?:[、和及与](?:'+labels+r'))*)\s*[：:]')
    matches=list(label_pattern.finditer(body));ranges={}
    date_pattern=r'(?:(?P<year>\d{4})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})日'
    for index,match in enumerate(matches):
        paragraph=body[match.end():matches[index+1].start() if index+1<len(matches) else len(body)]
        closure=paragraph.split('休市',1)
        if len(closure)!=2:
            raise ValueError('holiday_closure_missing')
        values=list(re.finditer(date_pattern,closure[0]))
        if not 1<=len(values)<=2:
            raise ValueError('ambiguous_holiday_dates')
        endpoints=[]
        for value in values:
            explicit=int(value['year']) if value['year'] else year
            if explicit!=year:
                raise ValueError('calendar_year_mismatch')
            endpoints.append(date(year,int(value['month']),int(value['day'])).isoformat())
        if len(endpoints)==2 and '至' not in closure[0]:
            raise ValueError('ambiguous_holiday_range')
        names=re.split('[、和及与]',match['label'])
        for name in names:
            if name in ranges:raise ValueError('duplicate_holiday')
            ranges[name]=[[endpoints[0],endpoints[-1]]]
    _build_days(year,ranges)
    published=set(re.findall(r'(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)',body[:matches[0].start()]))
    return {'closed_ranges':ranges,'published_at':next(iter(published)) if len(published)==1 else None,
            'source_document_hash':hashlib.sha256(raw).hexdigest()}


class _CalendarRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if not _trusted_source(newurl):
            raise ValueError('untrusted_calendar_redirect')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def _fetch_document(url):
    if not _trusted_source(url):raise ValueError('untrusted_calendar_source')
    request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 aq5-calendar','Accept-Encoding':'identity'})
    with urllib.request.build_opener(_CalendarRedirect()).open(request,timeout=12) as response:
        if not _trusted_source(response.geturl()):raise ValueError('untrusted_calendar_response')
        if response.headers.get('Content-Encoding','identity') not in ('','identity'):
            raise ValueError('unsupported_calendar_encoding')
        raw=response.read(MAX_DOCUMENT_BYTES+1)
    if len(raw)>MAX_DOCUMENT_BYTES:raise ValueError('calendar_document_too_large')
    return raw


def refresh_calendar(year,asof=None):
    """Fetch only the fixed official index and up to three exact-year annual links.

    Missing publication or an unrecognized format remains pending. Existing
    verified calendars are never replaced by partial data or guessed weekdays.
    """
    if type(year) is not int or not 2020<=year<=2100:raise ValueError('invalid_calendar_year')
    t=stamp(asof or now())
    if t is None:raise ValueError('invalid_observation_time')
    init();result=None
    try:
        raw=_fetch_document(CALENDAR_INDEX)
        candidates=[(CALENDAR_INDEX,raw)]
        html=_OfficialHTML();html.feed(raw.decode('utf-8-sig'))
        for link,label in html.links:
            if not re.search(rf'{year}年(?:全年)?休市安排',label):continue
            url=urllib.parse.urljoin(CALENDAR_INDEX,link)
            if _trusted_source(url) and url!=CALENDAR_INDEX and len(candidates)<4:
                candidates.append((url,None))
        errors=[]
        for source,document in candidates:
            try:
                document=document if document is not None else _fetch_document(source)
                parsed=parse_official_document(document,year)
                result=import_calendar(year,source=source,verified=True,observed_at=t,**parsed)
                break
            except (ValueError,UnicodeError,urllib.error.URLError,TimeoutError,OSError) as exc:
                errors.append(type(exc).__name__+':'+str(exc)[:120])
        if result is None:result={'year':year,'status':'pending','reason':errors[0] if errors else 'annual_notice_missing'}
    except (ValueError,UnicodeError,urllib.error.URLError,TimeoutError,OSError) as exc:
        result={'year':year,'status':'pending','reason':type(exc).__name__}
    result['retained_days']=len(official_days(year))
    with db.tx() as conn:
        conn.execute('INSERT OR REPLACE INTO forecast_calendar_refresh VALUES(?,?,?,?)',
                     (year,iso(t),result['status'],json.dumps(result,ensure_ascii=False)))
    return result


def status():
    return {'seed_years':[2026],'source':CALENDAR_INDEX,
            'years':db.q('SELECT year,source,published_at,observed_at,hash,source_document_hash FROM forecast_calendars ORDER BY year') if _table('forecast_calendars') else [],
            'refresh':db.q('SELECT * FROM forecast_calendar_refresh ORDER BY year') if _table('forecast_calendar_refresh') else [],
            'unknown_year_policy':'pending_official_annual_publication'}


def stamp(value) -> datetime | None:
    try:
        if isinstance(value, str):
            value = (datetime.strptime(value, "%Y%m%d%H%M%S") if len(value) == 14 and value.isdecimal()
                     else datetime.fromisoformat(value))
        if isinstance(value, datetime):
            return value.astimezone(CN_TZ) if value.tzinfo else value.replace(tzinfo=CN_TZ)
    except (ValueError, TypeError):
        pass
    return None


def iso(value) -> str:
    result = stamp(value)
    if result is None:
        raise ValueError("invalid timestamp")
    # Preserve observable event order. Truncating .900 after a quote was received
    # at .800 would incorrectly label that already-received quote as future data.
    return result.isoformat()


def dates(trading_days) -> list[str]:
    out = set()
    for item in trading_days or []:
        if isinstance(item, datetime):
            item = item.date()
        if isinstance(item, date):
            item = item.isoformat()
        try:
            out.add(date.fromisoformat(item).isoformat())
        except (ValueError, TypeError):
            raise ValueError("exchange calendar requires ISO dates") from None
    return sorted(out)


def normalize_horizon(value):
    aliases = {"d1":"1d", "d3":"3d", "d5":"5d", "d20":"20d", "m30":"30m", "m60":"60m"}
    return aliases.get(value, value)


def in_session(value, trading_days=None) -> bool:
    t = stamp(value)
    if t is None or (trading_days is not None and t.date().isoformat() not in dates(trading_days)):
        return False
    return any(start <= t.time().replace(tzinfo=None) < end for start, end in SESSIONS)


def due_at(asof, horizon: str, trading_days) -> str | None:
    """Return a deadline, or None when calendar/session evidence is insufficient.

    dN means the Nth complete exchange session after the origin date, at close.
    mN counts trading minutes across lunch but never carries into another day.
    Caller must supply a verified calendar even for intraday origins.
    """
    horizon = normalize_horizon(horizon)
    if horizon not in HORIZONS:
        raise ValueError("unsupported forecast horizon")
    t = stamp(asof)
    if t is None:
        raise ValueError("invalid asof")
    sessions = dates(trading_days)
    origin = t.date().isoformat()
    if origin not in sessions:
        return None
    if horizon.endswith("d"):
        index = sessions.index(origin) + int(horizon[:-1])
        if index >= len(sessions):
            return None
        return datetime.combine(date.fromisoformat(sessions[index]), time(15), CN_TZ).isoformat()
    if not in_session(t, sessions):
        return None
    remaining = timedelta(minutes=int(horizon[:-1]))
    cursor = t
    for start, end in SESSIONS:
        left = datetime.combine(t.date(), start, CN_TZ)
        right = datetime.combine(t.date(), end, CN_TZ)
        cursor = max(cursor, left)
        available = max(timedelta(), right - cursor)
        if remaining <= available:
            return iso(cursor + remaining)
        remaining -= available
        cursor = max(cursor, right)
    return None
