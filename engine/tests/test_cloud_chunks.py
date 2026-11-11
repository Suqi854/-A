"""Encrypted state transport: bounded Git blobs, immutable reads, intact history."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(os.environ['AQ5_DATA']).resolve()
BASE.mkdir(parents=True, exist_ok=True)
os.environ['AQ5_DATA'] = tempfile.mkdtemp(prefix='state-chunk-tests-', dir=BASE)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aq5 import cloud  # noqa: E402


class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix='case-', dir=os.environ['AQ5_DATA']))
        self.root = self.data / 'state_repo'
        self.root.mkdir()
        self.data_patch = patch.object(cloud, 'DATA', self.data)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)

    def files(self, blob, size=128):
        with patch.object(cloud, 'STATE_CHUNK_BYTES', size):
            cloud._write_state_chunks(self.root, blob)
        return {path.name:path.read_bytes() for path in self.root.iterdir() if path.is_file()}

    def read(self, files, size=128):
        calls = []
        def raw(path, ref=None):
            calls.append((path,ref))
            self.assertEqual(ref,'a'*40)
            return files.get(path)
        with patch.object(cloud,'_state_ref',return_value='a'*40) as resolve, \
                patch.object(cloud,'_gh_raw',side_effect=raw), patch.object(cloud,'STATE_CHUNK_BYTES',size):
            result = cloud.read_remote_state()
        resolve.assert_called_once()
        return result,calls

    def test_chunks_roundtrip_exact_bytes_at_one_immutable_commit(self):
        blob=b'AQ5v1'+bytes(range(256))*4
        files=self.files(blob)
        manifest=json.loads(files[cloud.STATE_MANIFEST])
        self.assertGreater(len(manifest['chunks']),1)
        self.assertEqual(manifest['sha256'],hashlib.sha256(blob).hexdigest())
        actual,calls=self.read(files)
        self.assertEqual(actual,blob)
        self.assertNotIn('state.bin',[path for path,ref in calls])
        self.assertTrue(all(ref=='a'*40 for path,ref in calls))

    def test_real_transport_limit_is_sixteen_mib(self):
        self.assertEqual(cloud.STATE_CHUNK_BYTES,16*1024*1024)
        blob=b'AQ5v1'+b'x'*(cloud.STATE_CHUNK_BYTES+12)
        files=self.files(blob,cloud.STATE_CHUNK_BYTES)
        manifest=json.loads(files[cloud.STATE_MANIFEST])
        self.assertEqual([part['size'] for part in manifest['chunks']],[16*1024*1024,17])
        self.assertTrue(all(len(value)<=16*1024*1024 for path,value in files.items()))

    def test_old_single_file_is_read_at_same_fixed_commit(self):
        blob=b'AQ5v1'+b'legacy'*20
        actual,calls=self.read({'state.bin':blob})
        self.assertEqual(actual,blob)
        self.assertEqual(calls,[(cloud.STATE_MANIFEST,'a'*40),('state.bin','a'*40)])

    def test_invalid_manifest_never_falls_back_to_old_state(self):
        files=self.files(b'AQ5v1'+b'x'*300)
        files['state.bin']=b'AQ5v1'+b'stale'*50
        original=json.loads(files[cloud.STATE_MANIFEST])
        for mutation in ('path','duplicate','size','hash','total','version','json_array'):
            value=json.loads(json.dumps(original))
            if mutation=='path':value['chunks'][0]['path']='../private.bin'
            elif mutation=='duplicate':value['chunks'][1]['path']=value['chunks'][0]['path']
            elif mutation=='size':value['chunks'][0]['size']=129
            elif mutation=='hash':value['sha256']='0'*64
            elif mutation=='total':value['size']+=1
            elif mutation=='version':value['version']=999
            else:value=[]
            bad={**files,cloud.STATE_MANIFEST:json.dumps(value).encode()}
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                self.read(bad)

    def test_missing_or_corrupt_chunk_refuses_the_whole_package(self):
        files=self.files(b'AQ5v1'+b'x'*300)
        for payload in (None,b'corrupt'):
            bad={**files,'state-part-00001.bin':payload}
            with self.subTest(payload=payload),self.assertRaisesRegex(ValueError,'integrity'):
                self.read(bad)

    def test_cleanup_only_removes_obsolete_app_owned_transport_files(self):
        (self.root/'state.bin').write_bytes(b'old ciphertext')
        (self.root/'state-part-00099.bin').write_bytes(b'old ciphertext')
        (self.root/'private-notes.txt').write_text('do not stage or remove')
        (self.root/'state-part-custom.bin').write_bytes(b'unrelated')
        self.files(b'AQ5v1'+b'x'*80)
        self.assertFalse((self.root/'state.bin').exists())
        self.assertFalse((self.root/'state-part-00099.bin').exists())
        self.assertEqual((self.root/'private-notes.txt').read_text(),'do not stage or remove')
        self.assertEqual((self.root/'state-part-custom.bin').read_bytes(),b'unrelated')
        with self.assertRaises(ValueError):cloud._state_path(self.root,'../state.bin')
        with self.assertRaises(ValueError):cloud._state_path(self.data,'state.bin')

    def test_metadata_and_manifest_use_resolved_commit(self):
        with patch.object(cloud,'_state_ref',return_value='b'*40), \
                patch.object(cloud,'_gh_raw',return_value=b'{"owner":"cloud","active":false}') as raw:
            meta=cloud.remote_meta()
        raw.assert_called_once_with('cloud.json',ref='b'*40)
        self.assertEqual(meta['_state_commit'],'b'*40)

    def test_push_failure_retains_specific_but_redacted_stderr(self):
        def git(args,cwd,timeout=120):
            if args==['remote']:return 0,'origin'
            if args[0]=='push':return 1,'remote: GH001: file exceeds size limit. fatal: https://user:credential@github.com/repo token=secret-value'
            return 0,''
        with patch.object(cloud,'_cfg',return_value={'remote':'fixture','repo':'fixture'}), \
                patch.object(cloud,'_key',return_value='fixture'),patch.object(cloud,'pack',return_value=b'AQ5v1'+b'x'*80), \
                patch.object(cloud,'_git',side_effect=git),patch.dict(os.environ,{'GITHUB_TOKEN':'secret-value'}):
            result=cloud.upload(active=True)
        self.assertFalse(result['ok'])
        self.assertIn('GH001',result['detail'])
        self.assertIn('size limit',result['detail'])
        for secret in ('credential','user:','secret-value','https://'):
            self.assertNotIn(secret,json.dumps(result))
        self.assertEqual(cloud.STATE['error_detail'],result['detail'])

    def test_real_git_amend_removes_legacy_blob_without_staging_unrelated_file(self):
        pushes=[]
        def git(args,cwd,timeout=120):
            if args[0]=='push':
                pushes.append(args)
                return 0,''  # No external/local server transport: inspect the actual committed tree below.
            run=subprocess.run(['git',*args],cwd=cwd,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
            return run.returncode,(run.stdout+run.stderr).strip()
        def must(args,cwd):
            rc,out=git(args,cwd);self.assertEqual(rc,0,out);return out
        remote=self.data/'remote.git';remote.mkdir()
        must(['init','--bare','-q'],remote)
        must(['init','-q'],self.root)
        must(['config','user.name','state-test'],self.root)
        must(['config','user.email','state-test@example.invalid'],self.root)
        must(['config','commit.gpgsign','false'],self.root)
        (self.root/'state.bin').write_bytes(b'AQ5v1'+b'old'*30)
        must(['add','state.bin'],self.root)
        must(['commit','-q','-m','legacy'],self.root)
        (self.root/'private-note.txt').write_text('never publish this')
        blob=b'AQ5v1'+b'history-unchanged'*30
        with patch.object(cloud,'_cfg',return_value={'remote':str(remote),'repo':'local'}), \
                patch.object(cloud,'_key',return_value='fixture'),patch.object(cloud,'pack',return_value=blob), \
                patch.object(cloud,'_git',side_effect=git),patch.object(cloud,'CLOUD',True),patch.object(cloud,'STATE_CHUNK_BYTES',128):
            result=cloud.upload(active=True)
        self.assertTrue(result['ok'],result)
        self.assertEqual(pushes,[['push','-q','--force','origin','HEAD:state']])
        names=must(['ls-tree','--name-only','HEAD'],self.root).splitlines()
        self.assertIn('cloud.json',names);self.assertIn(cloud.STATE_MANIFEST,names)
        self.assertNotIn('state.bin',names);self.assertNotIn('private-note.txt',names)
        self.assertEqual(must(['rev-list','--count','HEAD'],self.root),'1')
        self.assertEqual((self.root/'private-note.txt').read_text(),'never publish this')
        files={name:(self.root/name).read_bytes() for name in names}
        self.assertEqual(self.read(files)[0],blob)


if __name__=='__main__':unittest.main()
