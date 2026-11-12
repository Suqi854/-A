"""Synthetic full-universe contracts: denominator and source qualification are separate."""
import copy,io,json,os,sys,tempfile,unittest
from contextlib import ExitStack,redirect_stdout
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch

os.environ['AQ5_DATA']=os.environ['AQ5_TEST_DATA']
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aq5 import decision_full as full,decision_collect,decision_evidence as evidence
from aq5 import config,db,market,decision_snapshot,decision_universe,decision_growth,decision_emotion

AT=datetime.fromisoformat('2026-11-12T10:00:00+08:00')


def fixture():
    codes=['sh600000','sh600001','sz000001','sz000002']
    official=[{'code':c,'name':'合成样例'+str(i),'is_st_name_marker':i==0,'suspended':None} for i,c in enumerate(codes)]
    universe={'rows':official,'verified':True,'coverage':{'sse':2,'szse':2,'st_name_markers':1}}
    rows=[{**r,'price':10.,'quote_status':'available','research_exclusion':'ST名称风险标记' if r['is_st_name_marker'] else None} for r in official]
    growth={'records':[],'failures':[],'coverage':{'all_requested_attempted':True},'status_by_code':{
        c:{'financial':{'status':'qualified','attempted':True,'fetched':True,'value':True,'reason':'fixture'},
           'catalyst':{'status':'qualified','attempted':True,'value':True}} for c in codes}}
    emotion={'records':[],'failures':[],'status':'complete','data_day':'2026-11-11','pending_tasks':[],
        'retryable_tasks':[],'coverage':{'data_complete':True},'code_outcomes':{
            c:{'mainline':False,'core':False,'cooling':False,'history':'qualified','membership_covered':True,
               'lhb':'not_listed_in_complete_window'} for c in codes}}
    return universe,rows,growth,emotion


class FullSummaryTests(unittest.TestCase):
    def setUp(self):
        self.inputs=fixture()
        self.ingestion={'inserted':0,'rejected':[],'active_ids':{}}
        stack=ExitStack();self.addCleanup(stack.close)
        self.ingest=stack.enter_context(patch.object(decision_collect,'_ingest',side_effect=lambda r:copy.deepcopy(self.ingestion)))
        self.observe=stack.enter_context(patch.object(evidence,'record_observation'))
        self.screen=stack.enter_context(patch.object(evidence,'screen',side_effect=lambda *a:{
            k:{'candidates':[],'watch':[],'candidate_count':0,'note':'fixture'} for k in ('value','emotion')}))
        stack.enter_context(patch.object(config,'now',return_value=AT))

    def summary(self):return full.make_summary(*self.inputs)

    def test_full_denominator_retains_st_and_false_is_evaluated(self):
        result=self.summary()
        self.assertEqual(result['scope']['universe_count'],4)
        self.assertEqual(result['scope']['accounted_count'],4)
        self.assertEqual(result['scope']['st_in_denominator'],1)
        self.assertTrue(result['scope']['scan_complete'])
        self.assertTrue(result['scope']['data_complete'])
        self.assertEqual(result['coverage']['emotion']['eligible'],4)
        self.assertEqual(result['coverage']['emotion']['unknown'],0)
        self.assertEqual(len(self.screen.call_args.args[0]),4)
        self.assertEqual(self.screen.call_args.args[0][0]['research_exclusion'],'ST名称风险标记')

    def test_financial_nonapplicability_is_not_a_missing_source(self):
        status=self.inputs[2]['status_by_code']['sh600000']
        status['financial'].update(status='nonapplicable',reason='nonpositive_yoy_base')
        status['financial'].pop('value')
        status['catalyst']={'status':'not_evaluated','attempted':False,'reason':'financial_unqualified'}
        result=self.summary()
        self.assertEqual(result['coverage']['value']['excluded'],1)
        self.assertEqual(result['coverage']['value']['unknown'],0)
        self.assertTrue(result['scope']['data_complete'])

    def test_missing_financial_and_catalyst_count_code_once(self):
        status=self.inputs[2]['status_by_code']['sh600000']
        status['financial'].update(status='missing',fetched=False)
        status['catalyst'].update(status='missing')
        result=self.summary()
        self.assertEqual(result['coverage']['value']['unknown'],1)
        self.assertTrue(result['scope']['scan_complete'])
        self.assertFalse(result['scope']['data_complete'])

    def test_required_catalyst_missing_or_malformed_qualified_is_unknown(self):
        for catalyst in ({'status':'not_evaluated','attempted':False}, {'status':'qualified','attempted':True}):
            with self.subTest(catalyst=catalyst):
                self.inputs=fixture();self.inputs[2]['status_by_code']['sh600000']['catalyst']=catalyst
                result=self.summary()
                self.assertEqual(result['coverage']['value']['unknown'],1)
                self.assertFalse(result['scope']['data_complete'])
        self.inputs=fixture()
        self.inputs[2]['status_by_code']['sh600000']['financial']['value']=False
        self.inputs[2]['status_by_code']['sh600000']['catalyst']={'status':'not_required','attempted':False}
        self.assertTrue(self.summary()['scope']['data_complete'])

    def test_unknown_status_cannot_be_treated_as_qualified(self):
        self.inputs[2]['status_by_code']['sh600000']['financial']['status']='unexpected_new_status'
        result=self.summary()
        self.assertEqual(result['coverage']['value']['unknown'],1)
        self.assertFalse(result['scope']['data_complete'])

    def test_missing_quote_row_keeps_ledger_and_invalidates_completeness(self):
        self.inputs[1].pop()
        result=self.summary()
        self.assertEqual(len(result['coverage_manifest']),4)
        self.assertEqual(result['scope']['input_row_issues']['missing'],['sz000002'])
        self.assertFalse(result['scope']['scan_complete'])
        self.assertFalse(result['scope']['data_complete'])
        self.assertEqual(result['scope']['quote_missing_count'],1)

    def test_absent_or_failed_emotion_codes_and_false_metadata_fail_closed(self):
        for mutation in ('absent','history','mapping','unknown','lhb','pending'):
            with self.subTest(mutation=mutation):
                self.inputs=fixture();emotion=self.inputs[3];row=emotion['code_outcomes']['sh600000']
                if mutation=='absent':emotion['code_outcomes'].pop('sh600000')
                elif mutation=='history':row['history']='fetch_or_parse_failed'
                elif mutation=='mapping':row['membership_covered']=False
                elif mutation=='unknown':row['core']='UNKNOWN'
                elif mutation=='lhb':row['lhb']='listing_pending_or_failed'
                else:emotion['pending_tasks']=['history:sh600000']
                result=self.summary()
                self.assertEqual(len(result['coverage_manifest']),4)
                self.assertFalse(result['scope']['data_complete'])

    def test_ingestion_failure_and_terminal_source_error_prevent_data_complete(self):
        self.ingestion['rejected']=[{'code':'sh600000','reason':'invalid evidence'}]
        result=self.summary()
        self.assertTrue(result['scope']['scan_complete'])
        self.assertFalse(result['scope']['data_complete'])
        self.ingestion['rejected']=[]
        self.inputs[2]['failures']=[{'code':'sh600000','reason':'terminal source error'}]
        self.assertFalse(self.summary()['scope']['data_complete'])

    def test_stale_quotes_and_duplicate_extra_rows_never_shrink_or_inflate_scope(self):
        self.inputs[1][1]['price']=float('inf')
        self.inputs[1].append(dict(self.inputs[1][0]))
        self.inputs[1].append({'code':'sh600999','name':'extra','price':10,'quote_status':'available'})
        result=self.summary()
        self.assertEqual(result['scope']['accounted_count'],4)
        self.assertEqual(result['scope']['input_row_issues']['duplicates'],['sh600000'])
        self.assertEqual(result['scope']['input_row_issues']['extra'],['sh600999'])
        self.assertFalse(result['scope']['data_complete'])
        self.assertEqual(result['scope']['quote_missing_count'],2)

    def test_build_guard_runs_before_database_initialization(self):
        with patch.dict(os.environ,{'AQ5_FULL_RESEARCH':'0'}),patch.object(db,'init') as init:
            with self.assertRaises(RuntimeError):full.build(persist=False)
        init.assert_not_called()

    def test_build_full_codes_resume_version_and_suspension_contract(self):
        Path(os.environ['AQ5_TEST_DATA']).mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=os.environ['AQ5_TEST_DATA']) as folder,ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,{'AQ5_FULL_RESEARCH':'1','AQ5_RESEARCH_BRANCH':'research-cache','AQ5_RESEARCH_BUILD_ID':''}))
            stack.enter_context(patch.object(config,'DATA',Path(folder)))
            stack.enter_context(patch.object(db,'init'))
            stack.enter_context(patch.object(decision_universe,'collect',return_value=self.inputs[0]))
            quotes={r['code']:{'code':r['code'],'price':10,'time':AT.isoformat(),'suspended':i==1} for i,r in enumerate(self.inputs[0]['rows'])}
            quote_mock=stack.enter_context(patch.object(market,'quotes',return_value=quotes))
            g=stack.enter_context(patch.object(decision_growth,'collect',return_value=self.inputs[2]))
            m=stack.enter_context(patch.object(decision_emotion,'collect_full',return_value=self.inputs[3]))
            publish=stack.enter_context(patch.object(decision_snapshot,'publish'))
            with redirect_stdout(io.StringIO()):first=full.build(persist=False,budget_seconds=17)
            self.assertEqual(set(g.call_args.args[0]),{r['code'] for r in self.inputs[0]['rows']})
            self.assertEqual(len(quote_mock.call_args.args[0]),4)
            self.assertIs(m.call_args.args[0][1]['suspended'],True)
            self.assertEqual(m.call_args.kwargs['budget_seconds'],17)
            first_id=m.call_args.kwargs['run_id']
            with redirect_stdout(io.StringIO()):full.build(persist=False,budget_seconds=17)
            self.assertEqual(m.call_args.kwargs['run_id'],first_id)
            self.assertIsNone(m.call_args.kwargs['asof'])
            with patch.object(decision_emotion,'FULL_VERSION','test-new-version'),redirect_stdout(io.StringIO()):full.build(persist=False,budget_seconds=17)
            self.assertNotEqual(m.call_args.kwargs['run_id'],first_id)
            self.assertIsNotNone(m.call_args.kwargs['asof'])
            publish.assert_not_called()
            self.assertTrue(first['scope']['scan_complete'])


class LatestScreenTests(unittest.TestCase):
    def cached(self,age_hours=1,scan_complete=True):
        summary=full.empty_summary()
        summary['generated_at']=(AT-timedelta(hours=age_hours)).isoformat()
        summary['scope']['scan_complete']=scan_complete
        return {'summary':summary,'commit':'synthetic-commit'}

    def call(self,cached,**kwargs):
        with patch.object(config,'now',return_value=AT),patch.object(decision_snapshot,'read_summary',return_value=cached),\
             patch.object(full,'relay',return_value={'ok':True,'build':{'phase':'complete'}}) as relay:
            result=full.latest_screen(**kwargs)
        return result,relay

    def test_no_cache_readonly_queries_status_without_start(self):
        result,relay=self.call({'status':'unavailable'},start_if_needed=False)
        relay.assert_called_once_with('status')
        self.assertFalse(result['scope']['data_complete'])
        self.assertEqual(result['routes']['value']['candidates'],[])

    def test_stale_readonly_queries_status_preserving_original_time(self):
        cached=self.cached(age_hours=7);stamp=cached['summary']['generated_at']
        result,relay=self.call(cached,start_if_needed=False)
        relay.assert_called_once_with('status')
        self.assertEqual(result['generated_at'],stamp)
        self.assertEqual(result['snapshot_commit'],'synthetic-commit')

    def test_fresh_completed_scan_does_not_dispatch_build(self):
        result,relay=self.call(self.cached(age_hours=1))
        relay.assert_called_once_with('status')
        self.assertTrue(result['scope']['scan_complete'])

    def test_default_stale_or_incomplete_scan_starts_independent_build(self):
        for cached in (self.cached(age_hours=7),self.cached(age_hours=1,scan_complete=False)):
            with self.subTest(scope=cached['summary']['scope']):
                _,relay=self.call(cached)
                relay.assert_called_once_with('start',{})

    def test_queue_view_omits_full_ledger_and_stays_under_limit(self):
        cached=self.cached()
        cached['summary']['coverage_manifest']={f'sh{i:06d}':{'audit':'x'*1000} for i in range(3195)}
        self.assertGreater(len(json.dumps(cached).encode()),512*1024)
        result,_=self.call(cached,start_if_needed=False)
        self.assertNotIn('coverage_manifest',result)
        self.assertLess(len(json.dumps(result,ensure_ascii=False).encode()),512*1024)


if __name__=='__main__':unittest.main()
