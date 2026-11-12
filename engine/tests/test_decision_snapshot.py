"""Research checkpoint boundaries using only isolated synthetic data."""
import gzip
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ['AQ5_DATA']=os.environ['AQ5_TEST_DATA']
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aq5 import decision_snapshot as s, vault


def bundle(entries):
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode='w:gz') as tar:
        for name,raw in entries:
            item=tarfile.TarInfo(name);item.size=len(raw)
            tar.addfile(item,io.BytesIO(raw))
    return output.getvalue()


class SnapshotTests(unittest.TestCase):
    def restore(self,root,raw):
        with patch.object(s,'manifest',return_value=('a'*40,{'checkpoint':{}})),patch.object(s,'decode_part',return_value=raw):
            # A nonempty descriptor is required before decode.
            with patch.object(s,'manifest',return_value=('a'*40,{'checkpoint':{'test':True}})):
                return s.restore(root,password='fixture')

    def test_only_research_files_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'decision-full').mkdir()
            for name in ('summary.json','summary.json.tmp','cache.tmp-42'):(root/'decision-full'/name).write_text('{}')
            (root/'aq5.sqlite3').write_text('account sentinel')
            raw=s.archive(root)
            with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as tar:
                self.assertEqual(tar.getnames(),['decision-full/summary.json'])

    def test_restore_roundtrip_preserves_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);account=root/'aq5.sqlite3';account.write_bytes(b'unchanged')
            result=self.restore(root,bundle([('decision-full/summary.json',b'{"ok":true}')]))
            self.assertTrue(result['restored']);self.assertEqual(account.read_bytes(),b'unchanged')
            self.assertEqual(json.loads((root/'decision-full/summary.json').read_bytes()),{'ok':True})

    def test_preflight_rejects_escape_without_partial_write(self):
        for bad in ('decision-full/../aq5.sqlite3','state/account.json','/decision-full/x','decision-full\\x'):
            with self.subTest(bad=bad),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);account=root/'aq5.sqlite3';account.write_bytes(b'unchanged')
                with self.assertRaises(ValueError):
                    self.restore(root,bundle([('decision-full/valid.json',b'{}'),(bad,b'bad')]))
                self.assertFalse((root/'decision-full/valid.json').exists())
                self.assertEqual(account.read_bytes(),b'unchanged')

    def test_duplicate_and_parent_conflict_rejected_before_write(self):
        for names in (('decision-full/a','decision-full/a'),('decision-full/a','decision-full/a/b')):
            with self.subTest(names=names),tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):self.restore(tmp,bundle([(n,b'x') for n in names]))
                self.assertEqual(list(Path(tmp).iterdir()),[])

    def test_tar_metadata_counts_toward_limit(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'MAX_ARCHIVE',512):
            with self.assertRaisesRegex(ValueError,'archive_too_large'):
                self.restore(tmp,bundle([('decision-full/a',b'x')]))
            self.assertEqual(list(Path(tmp).iterdir()),[])

    def test_chunk_size_sum_rejected_before_network(self):
        descriptor={'size':70,'parts':[{'path':'summary-0000.bin','size':69,'sha256':'a'*64}]}
        with patch.object(s,'read_file') as read:
            with self.assertRaisesRegex(ValueError,'chunk_manifest'):s.decode_part('a'*40,descriptor,'fixture')
            read.assert_not_called()

    def test_cipher_integrity_and_password(self):
        raw=vault.seal(b'synthetic research','fixture')
        descriptor={'size':len(raw),'sha256':s.sha(raw),'parts':[{'path':'summary-0000.bin','size':len(raw),'sha256':s.sha(raw)}]}
        with patch.object(s,'read_file',return_value=raw):
            self.assertEqual(s.decode_part('a'*40,descriptor,'fixture'),b'synthetic research')
            with self.assertRaises(ValueError):s.decode_part('a'*40,descriptor,'wrong')
        with patch.object(s,'read_file',return_value=raw[:-1]+bytes([raw[-1]^1])):
            with self.assertRaisesRegex(ValueError,'integrity'):s.decode_part('a'*40,descriptor,'fixture')

    def test_publish_only_research_ref_no_force_on_conflict(self):
        calls=[]
        def api(path,method='GET',payload=None,**kwargs):
            calls.append((path,method,payload))
            if method=='PATCH':raise RuntimeError('concurrent writer')
            return {'sha':'b'*40,'tree':{'sha':'c'*40}}
        with patch.object(s,'manifest',return_value=('a'*40,{'format':s.FORMAT,'branch':s.BRANCH})),patch.object(s,'api',side_effect=api):
            with self.assertRaisesRegex(RuntimeError,'concurrent writer'):
                s.publish(summary={'kind':'research_preview','routes':{}},password='fixture')
        refs=[r for r in calls if 'refs' in r[0]]
        self.assertEqual(len(refs),1)
        self.assertEqual(refs[0],('git/refs/heads/research-cache','PATCH',{'sha':'b'*40,'force':False}))


if __name__=='__main__':unittest.main()
