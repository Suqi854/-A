"""Official current SSE/SZSE mainboard A-share denominator, independent of quotes."""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from .config import DATA, CN_TZ, now

VERSION = "official-mainboard-universe-1"
SSE = "https://query.sse.com.cn/sseQuery/commonQuery.do"
SZSE = "https://www.szse.cn/api/report/ShowReport"
MAX_BYTES = 20 * 1024 * 1024


def _hash(blob):
    return hashlib.sha256(blob).hexdigest()


def _stamp(value):
    t = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if t.utcoffset() is None:
        raise ValueError("universe_time_requires_timezone")
    return t.astimezone(CN_TZ)


def _fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0",
        "Referer":"https://www.szse.cn/" if "szse.cn" in url else "https://www.sse.com.cn/"})
    with urllib.request.urlopen(request, timeout=25) as response:
        raw = response.read(MAX_BYTES+1)
    if len(raw) > MAX_BYTES:
        raise ValueError("official_directory_response_too_large")
    return raw


def _capture(label, url, folder):
    raw = _fetch(url)
    digest = _hash(raw)
    path = folder / "raw" / (digest + ".bin")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(raw)
    return {"id":label,"source_url":url,"retrieved_at":now().isoformat(timespec="seconds"),
            "raw_sha256":digest,"bytes":len(raw),"raw_path":str(path)}


def _read(source, folder):
    # A resumed checkpoint may move between Windows and a Linux Actions runner.
    # Content-addressed local paths are reconstructed, never taken from an old host.
    digest = source["raw_sha256"]
    if not isinstance(digest,str) or not re.fullmatch(r"[a-f0-9]{64}",digest):
        raise ValueError("universe_raw_hash_invalid")
    path = (folder / "raw" / (digest + ".bin")).resolve()
    if not path.is_relative_to((folder / "raw").resolve()):
        raise ValueError("universe_raw_path_outside_cache")
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES or _hash(raw) != source["raw_sha256"]:
        raise ValueError("universe_raw_integrity_failed")
    return raw


def _sse_url(page=1, size=2000):
    # type=inParams and explicit STOCK_TYPE are required by the current official page.
    return SSE + "?" + urllib.parse.urlencode({"STOCK_TYPE":"1","REG_PROVINCE":"","CSRC_CODE":"","STOCK_CODE":"",
        "sqlId":"COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L","COMPANY_STATUS":"2,4,5,7,8","type":"inParams",
        "isPagination":"true","pageHelp.cacheSize":1,"pageHelp.beginPage":page,"pageHelp.pageSize":size,"pageHelp.pageNo":page})


def _szse_url(kind, main=True):
    path = SZSE + ("/data" if kind == "JSON" else "")
    args = {"SHOWTYPE":kind,"CATALOGID":"1110","TABKEY":"tab1"}
    if main:
        args["selectModule"] = "main"
    if kind == "JSON":
        args["PAGENO"] = 1
    return path + "?" + urllib.parse.urlencode(args)


def _xlsx(raw):
    ns = {"s":"http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if sum(x.file_size for x in z.infolist()) > 40*1024*1024:
            raise ValueError("official_xlsx_uncompressed_limit")
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            shared = ["".join(x.itertext()) for x in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("s:si", ns)]
        rows = []
        for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).findall(".//s:row",ns):
            cells = {}
            for cell in row.findall("s:c",ns):
                key = re.match(r"[A-Z]+",cell.attrib["r"]).group()
                value = cell.find("s:v",ns)
                value = value.text if value is not None else ""
                if cell.attrib.get("t") == "inlineStr":
                    value = "".join(cell.find("s:is",ns).itertext())
                elif cell.attrib.get("t") == "s":
                    value = shared[int(value)]
                cells[key] = value
            rows.append(cells)
    if not rows or len(rows) > 10000:
        raise ValueError("official_xlsx_row_count_invalid")
    columns = rows[0]
    if not {"板块","A股代码","A股简称","A股上市日期","所属行业"} <= set(columns.values()):
        raise ValueError("szse_directory_columns_changed")
    return [{name:row.get(key,"") for key,name in columns.items()} for row in rows[1:]]


def _sse_json(raw):
    value = json.loads(raw)
    rows, page = value.get("result"), value.get("pageHelp",{})
    total = page.get("total")
    if not isinstance(rows,list) or type(total) is not int or not 1 <= total <= 10000:
        raise ValueError("sse_empty_or_invalid_directory")
    if any(r.get("STOCK_TYPE") != "1" or r.get("LIST_BOARD") != "1"
           or r.get("DELIST_DATE") not in (None,"","-") or not re.fullmatch(r"\d{6}",str(r.get("A_STOCK_CODE",""))) for r in rows):
        raise ValueError("sse_board_listing_contract_changed")
    return rows, total, page


def _parse(sources, folder):
    sources = [{**source,"raw_path":str(folder/"raw"/(source["raw_sha256"]+".bin"))} for source in sources]
    by_id = {r["id"]:r for r in sources}
    first, total, _ = _sse_json(_read(by_id["sse_first"],folder))
    if len(first) != min(total,25):
        raise ValueError("sse_first_page_incomplete")
    sse, totals = [], []
    for page in range(1, math.ceil(total / 2000)+1):
        rows, declared, meta = _sse_json(_read(by_id[f"sse_page_{page}"],folder))
        if declared != total or meta.get("pageNo") != page:
            raise ValueError("sse_pagination_drift")
        sse.extend(rows);totals.append(declared)
    sse_codes = {r["A_STOCK_CODE"] for r in sse}
    if len(sse) != total or len(sse_codes) != total or not {r["A_STOCK_CODE"] for r in first} <= sse_codes:
        raise ValueError("sse_directory_incomplete_or_duplicate")
    sz = json.loads(_read(by_id["szse_metadata"],folder))
    if not isinstance(sz,list) or not sz or sz[0].get("metadata",{}).get("name") != "A股列表":
        raise ValueError("szse_metadata_invalid")
    metadata = sz[0]["metadata"]
    day = str(metadata.get("subname","")).strip()
    datetime.strptime(day,"%Y-%m-%d")
    main = _xlsx(_read(by_id["szse_main"],folder))
    all_rows = _xlsx(_read(by_id["szse_all"],folder))
    szcodes = {r["A股代码"] for r in main}
    if (metadata.get("recordcount") != len(main) or len(szcodes) != len(main)
            or any(r["板块"] != "主板" or not re.fullmatch(r"\d{6}",r["A股代码"]) for r in main)
            or szcodes != {r["A股代码"] for r in all_rows if r["板块"] == "主板"}
            or not {r.get("agdm") for r in sz[0].get("data",[])} <= szcodes):
        raise ValueError("szse_directory_crosscheck_failed")
    rows = [{"code":"sh"+r["A_STOCK_CODE"],"name":r["SEC_NAME_CN"],"board":"主板","exchange":"SSE",
             "listed":r.get("LIST_DATE"),"industry_code":r.get("CSRC_CODE"),"industry":r.get("CSRC_CODE_DESC"),
             "industry_scheme":"CSRC_section","directory_date":day,"industry_source":"SSE official directory",
             "listing_state":r.get("STATE_CODE"),"stock_state":r.get("STATE_CODE_STOCK"),"product_status":r.get("PRODUCT_STATUS"),
             "suspended":None,"is_st_name_marker":"ST" in r["SEC_NAME_CN"].upper()} for r in sse]
    for r in main:
        industry = r["所属行业"].split(" ",1)
        rows.append({"code":"sz"+r["A股代码"],"name":r["A股简称"],"board":"主板","exchange":"SZSE",
                     "listed":r["A股上市日期"],"industry_code":industry[0],"industry":industry[-1],
                     "industry_scheme":"CSRC_section","directory_date":day,"industry_source":"SZSE official directory",
                     "listing_state":"official_current_A_share_directory","stock_state":"UNKNOWN","suspended":None,
                     "is_st_name_marker":"ST" in r["A股简称"].upper()})
    rows.sort(key=lambda r:r["code"])
    universe_hash = _hash(json.dumps(rows,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode())
    if len({r["code"] for r in rows}) != len(rows):
        raise ValueError("universe_duplicates")
    return {"rows":rows,"verified":True,"directory_date":day,"universe_hash":universe_hash,
            "coverage":{"status":"official_complete","universe_count":len(rows),"sse":len(sse),"szse":len(main),
                        "sse_declared_total":total,"sse_page_totals":totals,"szse_declared_total":metadata["recordcount"],
                        "szse_export_crosscheck":True,"st_name_markers":sum(r["is_st_name_marker"] for r in rows),
                        "quote_independent":True,"tradeability_verified":False},"sources":sources,"failures":[]}


def collect(asof=None, cache_hours=24, storage_dir=None):
    folder = Path(storage_dir) if storage_dir else DATA / "decision-universe"
    folder.mkdir(parents=True,exist_ok=True)
    current = _stamp(now()); requested = _stamp(asof) if asof is not None else current
    manifest = folder / "latest.json"
    previous = None
    try:
        if manifest.exists():
            previous = json.loads(manifest.read_text("utf-8"))
            age = current - _stamp(previous["asof"])
            if (previous.get("version") == VERSION and previous.get("directory_date") == requested.date().isoformat()
                    and timedelta(0) <= age <= timedelta(hours=cache_hours)):
                parsed = _parse(previous["sources"],folder)
                return {**parsed,"version":VERSION,"asof":previous["asof"],"cached":True}
        if requested.date() != current.date():
            raise ValueError("historical_directory_not_available_from_current_endpoint")
        sources = [_capture("sse_first",_sse_url(size=25),folder)]
        _, total, _ = _sse_json(_read(sources[0],folder))
        for page in range(1,math.ceil(total/2000)+1):
            sources.append(_capture(f"sse_page_{page}",_sse_url(page),folder))
        for label,kind,main in (("szse_metadata","JSON",True),("szse_main","xlsx",True),("szse_all","xlsx",False)):
            sources.append(_capture(label,_szse_url(kind,main),folder))
        result = _parse(sources,folder)
        if result["directory_date"] != requested.date().isoformat():
            raise ValueError("official_directory_date_mismatch")
        result.update(version=VERSION,asof=now().isoformat(timespec="seconds"),cached=False)
        temporary = manifest.with_suffix(".tmp")
        temporary.write_text(json.dumps(result,ensure_ascii=False,separators=(",",":")),"utf-8")
        temporary.replace(manifest)
        return result
    except (OSError,ValueError,KeyError,TypeError,IndexError,AttributeError,zipfile.BadZipFile,ET.ParseError) as exc:
        return {"version":VERSION,"rows":[],"verified":False,"asof":now().isoformat(timespec="seconds"),
                "coverage":{"status":"unavailable","universe_count":None,"quote_independent":True,
                            "previous_verified_count":(previous or {}).get("coverage",{}).get("universe_count")},
                "sources":locals().get("sources",[]),"failures":[{"reason":type(exc).__name__+":"+str(exc)[:180]}]}
