import os
import tempfile
import unittest
from pathlib import Path
from config import Settings
from integration.cleanup import cleanup


class CleanupTests(unittest.TestCase):
    def test_expiry_preserves_primary_files_and_unfinished_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            s=Settings(data_dir=Path(directory))
            for number in (1,2):
                folder=s.content_dir/str(number);folder.mkdir()
                for name in ('raw.txt','corrected.txt','summary.json','summary_manifest.json','part_1.txt','correction_0.txt','summary_part_0.json'):
                    path=folder/name;path.write_text('test');os.utime(path,(1,1))
            cache=s.cache_dir/'audio.wav';cache.write_text('audio');os.utime(cache,(1,1))
            result=cleanup(s,[{'id':1,'status':'done'},{'id':2,'status':'queued'}],now=1000000)
            self.assertEqual(result['files'],4)
            self.assertTrue((s.content_dir/'1'/'raw.txt').exists())
            self.assertTrue((s.content_dir/'1'/'summary_manifest.json').exists())
            self.assertTrue((s.content_dir/'2'/'part_1.txt').exists())
            self.assertEqual(cleanup(s,[],now=1000000)['files'],0)

    def test_capacity_evicts_oldest_cache_without_touching_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            s=Settings(data_dir=Path(directory));s.values['storage']['cache_max_mb']=1
            for name,stamp in [('old',100),('new',200)]:
                p=s.cache_dir/name;p.write_bytes(b'x'*700000);os.utime(p,(stamp,stamp))
            result=cleanup(s,[],now=300)
            self.assertEqual(result['files'],1)
            self.assertFalse((s.cache_dir/'old').exists())
            self.assertTrue((s.cache_dir/'new').exists())
