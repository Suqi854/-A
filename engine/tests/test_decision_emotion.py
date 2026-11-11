import os,sys,unittest
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

os.environ['AQ5_DATA']=os.environ['AQ5_TEST_DATA']
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aq5 import decision_emotion as em
from aq5.config import CN_TZ


def history():
    return [{'date':f'2026-10-{i+1:02d}','close':10+i*.2,'vol':100,'high':11+i*.2,'low':9+i*.2,'amount':1000} for i in range(30)]


class EvidenceTests(unittest.TestCase):
    def test_board_requires_multiday_and_breadth(self):
        b={'f3':5,'f104':8,'f105':2}
        self.assertTrue(em.board_signal(b,history())['eligible'])
        self.assertFalse(em.board_signal(b,history()[:3])['eligible'])
        self.assertFalse(em.board_signal({**b,'f104':2,'f105':8},history())['eligible'])
        self.assertFalse(em.board_signal({**b,'f104':'8'},history())['eligible'])

    def test_core_not_single_day_winner(self):
        members=[{'f24':i} for i in range(10)]
        self.assertTrue(em.core_signal(members[-1],members,history(),{'return20':.1})['eligible'])
        self.assertFalse(em.core_signal(members[0],members,history(),{'return20':.1})['eligible'])
        self.assertFalse(em.core_signal(members[-1],members,history()[:5],{'return20':.1})['eligible'])
        self.assertFalse(em.core_signal(members[-1],members[:3],history(),{'return20':.1})['eligible'])

    def test_cooling_needs_shrink_and_ma_support(self):
        h=history();members=[{'f24':i} for i in range(10)]
        self.assertFalse(em.core_signal(members[-1],members,h,{'return20':.1})['cooling'])
        for r in h[-3:]:r.update(close=14.7,vol=30)
        self.assertTrue(em.core_signal(members[-1],members,h,{'return20':.1})['cooling'])
        h[-1]['close']=8
        self.assertFalse(em.core_signal(members[-1],members,h,{'return20':.1})['cooling'])

    def test_seats_never_establish_hot_money_identity(self):
        rows=[{'SECURITY_CODE':'002384','TRADE_DATE':'2026-11-11 00:00:00','TRADE_ID':'1','CHANGE_TYPE':'a',
               'OPERATEDEPT_CODE':str(i),'OPERATEDEPT_NAME':name,'BUY':100,'SELL':40}
              for i,name in enumerate(('机构专用','深股通专用','某证券营业部','未知席位'))]
        proxy=em.seat_proxy(rows,'sz002384','2026-11-11')
        self.assertEqual(proxy['non_institution_buy'],100)
        self.assertEqual(proxy['non_institution_net'],60)
        self.assertEqual(proxy['hot_money_identity'],'UNKNOWN')
        self.assertFalse(proxy['eligible_hot_money_fact'])
        self.assertEqual(len(proxy['seats']),4)

    def test_overlapping_lhb_lists_not_double_counted(self):
        row={'SECURITY_CODE':'002384','TRADE_DATE':'2026-11-11','TRADE_ID':'1','CHANGE_TYPE':'a',
             'OPERATEDEPT_CODE':'1','OPERATEDEPT_NAME':'证券营业部','BUY':100,'SELL':40}
        rows=[row,row,{**row,'CHANGE_TYPE':'b','TRADE_ID':'2'}]
        self.assertEqual(em.seat_proxy(rows,'sz002384','2026-11-11')['non_institution_buy'],100)
        self.assertEqual(em.seat_proxy(rows,'sh600000','2026-11-11')['seats'],[])
        anonymous=[{**row,'OPERATEDEPT_CODE':'0','OPERATEDEPT_NAME':'机构专用'},
                   {**row,'OPERATEDEPT_CODE':'0','OPERATEDEPT_NAME':'机构专用','BUY':200}]
        self.assertEqual(len(em.seat_proxy(anonymous+anonymous,'sz002384','2026-11-11')['seats']),2)

    def test_record_expiry_is_source_fixed_and_known_time_not_backdated(self):
        at=datetime(2026,11,11,20,tzinfo=CN_TZ);src=at.replace(hour=15)
        receipt={'source_url':'https://data.eastmoney.com/stock/tradedetail.html','raw_sha256':'a'*64,
                 'retrieved_at':(at+timedelta(seconds=1)).isoformat()}
        a=em._record('sz002384','core','test',src,[receipt],at)
        b=em._record('sz002384','core','test',src,[receipt],at+timedelta(hours=1))
        self.assertEqual(a['expires_at'],b['expires_at'])
        self.assertGreater(a['available_at'],at.isoformat())
        self.assertIsNone(em._record('sz002384','core','test',src+timedelta(days=1),[receipt],at))

    def test_failed_sources_return_coverage_and_unknown_not_true(self):
        with patch.object(em,'_boards',side_effect=RuntimeError('blocked')),patch.object(em,'_get',side_effect=RuntimeError('blocked')):
            result=em.collect([{'code':'sz002384','name':'test'}])
        self.assertEqual(result['records'],[])
        self.assertEqual(result['coverage']['hot_money_identified'],0)
        self.assertGreaterEqual(len(result['failures']),2)
        self.assertFalse(result['full_market_screen'])

    def test_complete_reassessment_emits_false_instead_of_reviving_old_core(self):
        at=datetime(2026,11,11,20,tzinfo=CN_TZ)
        receipt={'source_url':'https://finance.sina.com.cn/test','raw_sha256':'a'*64,'retrieved_at':at.isoformat()}
        members=[{'symbol':f'sh60000{i}'} for i in range(1,7)]
        bars=[{'date':(at-timedelta(days=64-i)).date().isoformat(),'close':10+i*.2,'vol':100} for i in range(65)]
        def get(url,**kwargs):
            return ({'new_test':'new_test,测试行业,6,1,1,5,1,1,sh600001,1,1,1,测试'} if kwargs.get('sina_var') else members),receipt
        with patch.object(em,'_get',side_effect=get),patch.object(em,'_history',return_value=(bars,receipt)):
            records,*_=em._sina_fallback({'sh600001'},at,3,30)
        values={r['kind']:r['value'] for r in records}
        self.assertIs(values['core'],False)
        self.assertIs(values['cooling'],False)
        with patch.object(em,'_get',side_effect=lambda *a,**k:({'new_test':'new_test,测试行业,7,1,1,5,1,1,sh600001,1,1,1,测试'} if k.get('sina_var') else members,receipt)):
            records,*_=em._sina_fallback({'sh600001'},at,3,30)
        self.assertEqual(records,[])

    def test_context_selection_never_splices_boards_and_is_order_independent(self):
        def r(board,kind,value):return {'code':'sz002384','kind':kind,'value':value,'provenance':{'context_id':board}}
        records=[r('weak','mainline',False),r('weak','core',True),r('weak','cooling',True),r('strong','mainline',True)]
        first,context=em.select_context(records)
        second,_=em.select_context(list(reversed(records)))
        self.assertEqual(first,second)
        self.assertEqual([r['kind'] for r in first],['mainline'])
        self.assertEqual(context[0]['selected_context'],'strong')
        complete=[r('complete','mainline',True),r('complete','core',True),r('complete','cooling',False)]
        result,_=em.select_context(records+complete)
        self.assertEqual({r['provenance']['context_id'] for r in result},{'complete'})

    def focused_lhb(self, pages, focus_codes, *, fail_page=None):
        at=datetime(2026,11,11,20,tzinfo=CN_TZ)
        other=[f'sh600{i:03d}' for i in range(1,13)]
        snapshot=[{'code':c,'name':'样例'} for c in other+['sz002384','sz002916']]
        requests=[]
        def get(url,**kwargs):
            q={k:v[0] for k,v in parse_qs(urlsplit(url).query).items()};requests.append(q)
            receipt={'source_url':url,'raw_sha256':'a'*64,'retrieved_at':at.isoformat()}
            if q['reportName']=='RPT_DAILYBILLBOARD_DETAILSNEW':
                page=int(q['pageNumber'])
                if page==fail_page:raise RuntimeError('page unavailable')
                return {'success':True,'result':{'pages':len(pages),'data':[
                    {'SECURITY_CODE':c[2:],'TRADE_DATE':'2026-11-11 00:00:00'} for c in pages[page-1]]}},receipt
            code=next(c for c in other+['sz002384','sz002916'] if c[2:] in q['filter'])
            return {'success':True,'result':{'pages':1,'data':[{'SECURITY_CODE':code[2:],'TRADE_DATE':'2026-11-11',
                'TRADE_ID':'1','CHANGE_TYPE':'a','OPERATEDEPT_CODE':'1','OPERATEDEPT_NAME':'证券营业部','BUY':100,'SELL':40}]}},receipt
        with patch.object(em,'_boards',return_value=([],[],0,True)),patch.object(em,'_sina_fallback',return_value=([],[],[],[],{})),\
             patch.object(em,'_focus_fallback',return_value=([],[],[],[])),patch.object(em,'_get',side_effect=get):
            result=em.collect(snapshot,at,focus_codes=focus_codes)
        return result,requests,other

    def test_focus_lhb_after_twelve_other_stocks_is_not_starved(self):
        others=[f'sh600{i:03d}' for i in range(1,13)]
        result,requests,_=self.focused_lhb([others+['sz002384']],['sz002384'])
        self.assertEqual(result['coverage']['lhb_candidates'],1)
        self.assertEqual([r['code'] for r in result['proxy_metrics']],['sz002384'])
        self.assertTrue(all('(SECURITY_CODE="002384")' in q['filter'] for q in requests))
        self.assertEqual(len(requests),3)
        # The same list without focus retains the existing bounded market behavior.
        screen,_,_=self.focused_lhb([others+['sz002384']],None)
        self.assertEqual(screen['coverage']['lhb_details'],12)
        self.assertNotIn('sz002384',[r['code'] for r in screen['proxy_metrics']])

    def test_focus_lhb_pages_and_multiple_exact_codes(self):
        others=[f'sh600{i:03d}' for i in range(1,13)]
        result,requests,_=self.focused_lhb([others,['sz002384','sz002916']],['sz002384','sz002916'])
        self.assertEqual({r['code'] for r in result['proxy_metrics']},{'sz002384','sz002916'})
        lists=[q for q in requests if q['reportName']=='RPT_DAILYBILLBOARD_DETAILSNEW']
        self.assertEqual(len(lists),4)
        self.assertEqual(sorted(q['pageNumber'] for q in lists),['1','1','2','2'])
        self.assertTrue(result['coverage']['lhb_list_complete'])

    def test_focus_missing_or_page_failure_stays_unknown(self):
        others=[f'sh600{i:03d}' for i in range(1,13)]
        result,requests,_=self.focused_lhb([others],['sz002384'])
        self.assertEqual(result['proxy_metrics'],[])
        self.assertEqual(len(requests),1)
        failed,requests,_=self.focused_lhb([others,['sz002384']],['sz002384'],fail_page=2)
        self.assertEqual(failed['proxy_metrics'],[])
        self.assertFalse(failed['coverage']['lhb_list_complete'])

if __name__=='__main__':unittest.main()
