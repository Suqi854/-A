import os,sys,unittest,json,uuid,hashlib
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


class FullScopeTests(unittest.TestCase):
    def setUp(self):
        self.at=datetime(2026,11,11,20,tzinfo=CN_TZ)
        self.run_id='unit-'+uuid.uuid4().hex
        self.codes=[f'sh60000{i}' for i in range(1,7)]
        self.snapshot=[{'code':c,'name':'*ST样例' if i==0 else '样例','is_st_name_marker':i==0,
                        'suspended':True if i==1 else None,'industry':'制造业','industry_scheme':'CSRC_section'} for i,c in enumerate(self.codes)]
        raw=json.dumps({'synthetic_fixture':self.run_id}).encode()
        sha=hashlib.sha256(raw).hexdigest();root=Path(em.DATA)/'decision-public'/'emotion';root.mkdir(parents=True,exist_ok=True)
        self.raw_path=root/(sha+'.json');self.raw_path.write_bytes(raw)
        self.receipt={'source_url':'https://finance.sina.com.cn/synthetic-fixture','raw_sha256':sha,
                      'raw_path':str(self.raw_path),'retrieved_at':self.at.isoformat()}
        self.bars=[{'date':(self.at-timedelta(days=64-i)).date().isoformat(),'close':10+i*.4,'vol':100,'high':11+i*.4,'low':9+i*.4,'amount':1000} for i in range(65)]

    def catalog(self,kind,fetch):
        return {'boards':[{'id':'sina:'+kind+':unit','node':'new_unit','name':'合成行业','kind':kind,
                           'scope':'合成测试','catalog_display_count':7}],'receipts':[self.receipt]}

    def members(self,board,fetch):
        return {'board':board,'source_total':7,'source_symbols':self.codes+['sh600999'],
                'mainboard_members':self.codes+['sh600999'],'non_mainboard_count':0,
                'source_membership_complete':True,'receipts':[self.receipt]}

    def history(self,code,at,**kwargs):return self.bars,self.receipt
    def listing(self,at,fetch):return {'rows':[],'receipts':[self.receipt],'complete':True,'row_count':0}

    def collect(self,**kwargs):
        defaults={'run_id':self.run_id,'asof':self.at,'max_workers':2,'budget_seconds':30}
        defaults.update(kwargs)
        with patch.object(em,'_full_catalog',side_effect=self.catalog),patch.object(em,'_full_members',side_effect=self.members),\
             patch.object(em,'_history',side_effect=self.history) as histories,patch.object(em,'_full_lhb_listing',side_effect=self.listing):
            result=em.collect_full(self.snapshot,**defaults)
        return result,histories.call_count

    def test_all_codes_st_halt_and_shared_history_are_preserved(self):
        progress=[];result,calls=self.collect(progress=progress.append)
        self.assertEqual(result['coverage']['universe_count'],6)
        self.assertEqual(len(result['code_outcomes']),6)
        self.assertEqual(calls,7) # six stocks plus one common completed-day benchmark, not per context
        self.assertTrue(result['code_outcomes'][self.codes[0]]['is_st_name_marker'])
        self.assertIs(result['code_outcomes'][self.codes[1]]['suspended'],True)
        self.assertEqual(result['source_mainboard_outside_universe'],['sh600999'])
        self.assertTrue(all(o['lhb']=='not_listed_in_complete_window' for o in result['code_outcomes'].values()))
        self.assertTrue(result['coverage']['data_complete'])
        self.assertFalse(result['full_market_screen'])
        self.assertEqual({p['stage'] for p in progress},{'sources','memberships','daily_histories','lhb_details'})

    def test_completed_run_resumes_without_network_or_new_cutoff(self):
        first,_=self.collect()
        second,calls=self.collect(asof=None)
        self.assertEqual(calls,0)
        self.assertEqual(second['asof'],first['asof'])
        self.assertEqual(second['data_day'],first['data_day'])
        with self.assertRaises(ValueError):self.collect(asof=self.at+timedelta(minutes=1))

    def test_unmapped_and_new_history_remain_unknown_without_lost_denominator(self):
        original_members=self.members; original_history=self.history
        def members(board,fetch):
            result=original_members(board,fetch);result['mainboard_members']=self.codes[:-1];return result
        self.members=members
        self.history=lambda code,at,**kwargs:(self.bars[-10:],self.receipt) if code==self.codes[0] else original_history(code,at,**kwargs)
        result,calls=self.collect()
        self.assertEqual(calls,7)
        self.assertEqual(result['unmapped_codes'],[self.codes[-1]])
        self.assertEqual(result['code_outcomes'][self.codes[0]]['history'],'insufficient_history')
        self.assertEqual(result['code_outcomes'][self.codes[0]]['core'],'UNKNOWN')
        self.assertEqual(result['coverage']['accounted_count'],6)
        self.assertFalse(result['coverage']['data_complete'])

    def test_budget_checkpoint_never_claims_coverage_and_can_resume(self):
        with patch.object(em._FullRun,'check',side_effect=em._BudgetEnd()):
            result,_=self.collect()
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(len(result['code_outcomes']),6)
        self.assertEqual(result['coverage']['history_checked'],0)
        self.assertTrue(result['pending_tasks'])
        resumed,_=self.collect(asof=None)
        self.assertTrue(resumed['coverage']['data_complete'])

    def test_source_failures_are_bounded_and_do_not_create_false(self):
        self.members=lambda *a:(_ for _ in ()).throw(RuntimeError('provider failed'))
        first,_=self.collect();second,_=self.collect(asof=None)
        self.assertEqual(first['status'],'incomplete')
        self.assertEqual(second['status'],'complete_with_gaps')
        self.assertTrue(all(o['mainline']=='UNKNOWN' for o in second['code_outcomes'].values()))
        self.assertTrue(all(o['history']=='qualified' for o in second['code_outcomes'].values()))

    def test_raw_checkpoint_missing_cannot_be_reused(self):
        self.collect()
        run=em._FullRun(self.snapshot,None,self.run_id,1,30)
        self.assertIsNotNone(run.read('history:'+self.codes[0]))
        self.raw_path.unlink()
        self.assertIsNone(run.read('history:'+self.codes[0]))

    def test_lhb_all_pages_requires_row_count_and_rejects_repeat(self):
        def fetch(url,**kwargs):
            page=int(parse_qs(urlsplit(url).query)['pageNumber'][0])
            rows=[{'row':i} for i in (range(500) if page==1 else [500])]
            return {'success':True,'result':{'pages':2,'count':501,'data':rows}},self.receipt
        result=em._full_lhb_pages({'reportName':'fixture'},fetch)
        self.assertEqual(result['row_count'],501)
        def repeated(url,**kwargs):
            result,receipt=fetch(url)
            if int(parse_qs(urlsplit(url).query)['pageNumber'][0])==2:result['result']['data']=[{'row':0}]
            return result,receipt
        with self.assertRaises(ValueError):em._full_lhb_pages({'reportName':'fixture'},repeated)

    def test_large_membership_full_pages_and_count_drift(self):
        board={'node':'new_unit','catalog_display_count':100}
        def fetch(url,**kwargs):
            if 'StockCount' in url:return '211',self.receipt
            page=int(parse_qs(urlsplit(url).query)['page'][0]);start=(page-1)*100
            return [{'symbol':f'sh{600000+i}'} for i in range(start,min(start+100,211))],self.receipt
        result=em._full_members(board,fetch)
        self.assertEqual(result['source_total'],211)
        self.assertEqual(len(result['source_symbols']),211)
        self.assertTrue(result['catalog_count_is_display_only'])
        def drift(url,**kwargs):
            if kwargs.get('phase')=='after-pages':return '212',self.receipt
            return fetch(url,**kwargs)
        with self.assertRaises(ValueError):em._full_members(board,drift)
        def variable_pages(url,**kwargs):
            if 'StockCount' in url:return '211',self.receipt
            page=int(parse_qs(urlsplit(url).query)['page'][0]);bounds={1:(0,100),2:(100,196),3:(196,211)}
            start,end=bounds[page]
            return [{'symbol':f'sh{600000+i}'} for i in range(start,end)],self.receipt
        self.assertEqual(em._full_members(board,variable_pages)['source_total'],211)

    def test_aligned_but_stale_anchor_does_not_make_complete_current_evidence(self):
        for row in self.bars:
            row['date']=(datetime.fromisoformat(row['date'])-timedelta(days=10)).date().isoformat()
        result,_=self.collect()
        self.assertFalse(result['coverage']['anchor_qualified'])
        self.assertFalse(result['coverage']['data_complete'])
        self.assertEqual(result['records'],[])
        self.assertTrue(all(o['history']=='benchmark_stale_or_unavailable' for o in result['code_outcomes'].values()))

    def test_bad_lhb_listing_cannot_turn_into_not_listed(self):
        for row in ({'SECURITY_CODE':'600001','TRADE_DATE':'bad'},
                    {'SECURITY_CODE':None,'TRADE_DATE':'2026-11-11'},
                    {'SECURITY_CODE':'600001','TRADE_DATE':'2026-11-12'}):
            with self.subTest(row=row),patch.object(em,'_full_lhb_pages',return_value={'rows':[row]}):
                with self.assertRaises(ValueError):em._full_lhb_listing(self.at,lambda *a:None)
        with patch.object(em,'_full_lhb_pages',return_value={'rows':[]}) as fetch:
            result=em._full_lhb_listing(self.at.replace(hour=10),lambda *a:None)
        self.assertEqual(result['window']['end'],'2026-11-10')
        self.assertIn("TRADE_DATE<='2026-11-10'",fetch.call_args.args[0]['filter'])

if __name__=='__main__':unittest.main()
