"""Official-directory contract counterexamples; no mock quotes define the denominator."""
import io, json, os, sys, tempfile, unittest, zipfile, shutil
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch
from xml.sax.saxutils import escape
os.environ['AQ5_DATA']=os.environ['AQ5_TEST_DATA']
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aq5 import decision_universe as u
AT=datetime.fromisoformat('2026-11-12T11:00:00+08:00')

def xlsx(rows):
    header=['板块','A股代码','A股简称','A股上市日期','所属行业']
    xml=['<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    for i,row in enumerate([header]+rows,1):
        xml.append(f'<row r="{i}">')
        for j,v in enumerate(row):xml.append(f'<c r="{chr(65+j)}{i}" t="inlineStr"><is><t>{escape(v)}</t></is></c>')
        xml.append('</row>')
    xml.append('</sheetData></worksheet>');out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:z.writestr('xl/worksheets/sheet1.xml',''.join(xml))
    return out.getvalue()

def fixture():
    row={'A_STOCK_CODE':'600000','SEC_NAME_CN':'*ST测试','STOCK_TYPE':'1','LIST_BOARD':'1','DELIST_DATE':'-',
         'CSRC_CODE':'J','CSRC_CODE_DESC':'金融业','LIST_DATE':'19991110','STATE_CODE':'7','STATE_CODE_STOCK':'8'}
    main=[['主板','000001','平安银行','1991-04-03','J 金融业']]
    allrows=main+[['创业板','300001','非主板','2009-10-30','C 制造业']]
    sse={'result':[row],'pageHelp':{'total':1,'pageNo':1,'pageCount':1}}
    meta=[{'metadata':{'name':'A股列表','subname':'2026-11-12 ','recordcount':1},'data':[{'agdm':'000001'}]}]
    return {'sse':sse,'metadata':meta,'main':main,'all':allrows}

class UniverseTests(unittest.TestCase):
    def setUp(self):
        Path(os.environ['AQ5_TEST_DATA']).mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=os.environ['AQ5_TEST_DATA']);self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.input=fixture();self.calls=[]
    def fetch(self,url):
        self.calls.append(url)
        if 'sse.com.cn' in url:return json.dumps(self.input['sse']).encode()
        if '/data?' in url:return json.dumps(self.input['metadata']).encode()
        return xlsx(self.input['main'] if 'selectModule=main' in url else self.input['all'])
    def collect(self,**kw):
        with patch.object(u,'now',return_value=AT),patch.object(u,'_fetch',side_effect=self.fetch):
            return u.collect(storage_dir=self.folder,**kw)
    def test_official_board_fields_totals_crosscheck_and_st_preserved(self):
        result=self.collect();self.assertTrue(result['verified'],result)
        self.assertEqual(result['coverage']['universe_count'],2)
        self.assertEqual(result['coverage']['st_name_markers'],1)
        self.assertEqual(result['rows'][0]['code'],'sh600000')
        self.assertIsNone(result['rows'][0]['suspended'])
        self.assertEqual(result['rows'][1]['industry_scheme'],'CSRC_section')
        self.assertEqual(len(result['sources']),5)
        self.assertTrue(all((self.folder/'raw'/f"{s['raw_sha256']}.bin").exists() for s in result['sources']))
    def test_sse_wrong_board_cannot_be_rescued_by_code_prefix(self):
        self.input['sse']['result'][0]['LIST_BOARD']='8'
        self.assertFalse(self.collect()['verified'])
    def test_sse_delisted_member_rejects_whole_directory_not_silent_filter(self):
        self.input['sse']['result'][0]['DELIST_DATE']='2026-11-11'
        self.assertFalse(self.collect()['verified'])
    def test_sse_declared_total_mismatch_is_not_complete(self):
        self.input['sse']['pageHelp']['total']=2
        result=self.collect();self.assertFalse(result['verified']);self.assertIsNone(result['coverage']['universe_count'])
    def test_sse_empty_response_is_failure(self):
        self.input['sse']={'result':[],'pageHelp':{'total':0}}
        self.assertFalse(self.collect()['verified'])
    def test_szse_cross_export_disagreement_rejects(self):
        self.input['all'][0][1]='000002'
        self.assertFalse(self.collect()['verified'])
    def test_szse_metadata_count_disagreement_rejects(self):
        self.input['metadata'][0]['metadata']['recordcount']=2
        self.assertFalse(self.collect()['verified'])
    def test_szse_old_directory_date_is_not_today(self):
        self.input['metadata'][0]['metadata']['subname']='2026-11-11'
        self.assertFalse(self.collect()['verified'])
    def test_cache_is_reparsed_and_hash_verified(self):
        first=self.collect();calls=len(self.calls);second=self.collect()
        self.assertTrue(second['cached']);self.assertEqual(len(self.calls),calls)
        Path(first['sources'][0]['raw_path']).write_bytes(b'tampered')
        self.assertFalse(self.collect()['verified'])
    def test_checkpoint_cache_is_portable_between_host_paths(self):
        first=self.collect()
        with tempfile.TemporaryDirectory(dir=os.environ['AQ5_TEST_DATA']) as directory:
            restored=Path(directory)/'restored';shutil.copytree(self.folder,restored)
            with patch.object(u,'now',return_value=AT),patch.object(u,'_fetch',side_effect=AssertionError('cache should be reusable')):
                result=u.collect(storage_dir=restored)
            self.assertTrue(result['verified']);self.assertTrue(result['cached'])
            self.assertEqual(result['universe_hash'],first['universe_hash'])
            self.assertTrue(all(Path(source['raw_path']).is_relative_to(restored) for source in result['sources']))
    def test_failure_preserves_last_verified_cache_but_does_not_claim_current(self):
        first=self.collect();saved=(self.folder/'latest.json').read_bytes()
        with patch.object(u,'now',return_value=AT+timedelta(days=1)),patch.object(u,'_fetch',side_effect=OSError('offline')):
            result=u.collect(storage_dir=self.folder)
        self.assertFalse(result['verified']);self.assertEqual(result['rows'],[])
        self.assertEqual(result['coverage']['previous_verified_count'],2)
        self.assertEqual((self.folder/'latest.json').read_bytes(),saved)
    def test_historical_date_never_uses_current_directory(self):
        result=self.collect(asof=AT-timedelta(days=1));self.assertFalse(result['verified']);self.assertEqual(self.calls,[])

if __name__=='__main__':unittest.main()
