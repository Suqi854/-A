"""Bounded public-data cloud acceptance; never loads accounts, vault or state.bin."""
from __future__ import annotations
import argparse,json,time
from pathlib import Path
from ..config import now,MOCK
from . import data,chan,features


def run():
    result={'at':now().isoformat(),'symbol':'sh600000','mock':MOCK,'scope':'one_public_symbol_one_run',
            'periods':{},'qualification':'not_continuous_or_profit_validation'}
    for tf in ('day','60m','15m','5m'):
        started=time.monotonic()
        try:
            feed=data.fetch('sh600000',tf)
            structure=chan.analyze('sh600000',tf,feed['rows'],now().isoformat())
            result['periods'][tf]={'source':feed['source'],'bars':len(feed['rows']),
                'last_bar':feed['rows'][-1]['date'] if feed['rows'] else None,'quality':feed['quality'],
                'structure_quality':structure['quality'],'confirmed':structure['confirmed'],
                'counts':{k:len(v) for k,v in structure['structures'].items()},
                'seconds':round(time.monotonic()-started,2)}
        except Exception as exc:
            result['periods'][tf]={'error':type(exc).__name__,'seconds':round(time.monotonic()-started,2)}
    result['engine']=chan.verify_vendor()
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    args=parser.parse_args();report=run();out=Path(args.output)
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
