import json
import io
import importlib.util
import os
from pathlib import Path
import re
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import patch

import web_app as web
from test_precision import review_fixture
from ocr_pipeline.review import ReviewConflict, ReviewStore


class ReviewRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.storage=patch.object(web,"DATA_DIR",self.root);self.storage.start()
        self.client=web.app.test_client()
        home=self.client.get("/")
        self.csrf=re.search(r'name="csrf-token" content="([^"]+)"',home.text)[1]
        with self.client.session_transaction() as session:self.owner=session["owner_id"]
        snapshot,payload=review_fixture(self.root)
        self.store=ReviewStore(self.root/"reviews")
        self.doc=self.store.register(self.owner,payload,snapshot)
        self.url=f"/reviews/{self.doc}"
    def tearDown(self):self.storage.stop();self.temporary.cleanup()
    def save(self,**extra):
        body={"version":1,"reviewer":"Synthetic tester","changes":[{"span_id":"P0001-test","status":"approved","text":"0046"}],**extra}
        return self.client.post(self.url,json=body,headers={"X-CSRF-Token":self.csrf})

    def test_owner_only_for_metadata_image_edits_and_search(self):
        other=web.app.test_client();home=other.get("/")
        csrf=re.search(r'name="csrf-token" content="([^"]+)"',home.text)[1]
        for suffix in ("","/pages/1","/search"):
            self.assertEqual(other.get(self.url+suffix).status_code,404)
        self.assertEqual(other.post(self.url,json={"version":1},headers={"X-CSRF-Token":csrf}).status_code,404)
        self.assertEqual(other.get("/reviews/data").json["documents"],[])
        self.assertEqual(self.client.get(self.url).status_code,200)
        with self.client.get(self.url+"/pages/1") as response:
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.mimetype,"image/png")

    def test_csrf_concurrency_limits_and_no_inline_script_injection(self):
        self.assertEqual(self.client.post(self.url,json={}).status_code,403)
        self.assertEqual(self.save().status_code,200)
        self.assertEqual(self.save().status_code,409)
        self.assertEqual(self.save(version=2,changes=[{"span_id":"P0001-test","status":"approved","text":"x"*10001}]).status_code,400)
        self.assertEqual(self.save(version=2,changes=[{"span_id":[],"status":"approved","text":"0046"}]).status_code,400)
        self.assertEqual(self.client.post(self.url,data='{"x":"'+'a'*140000+'"}',headers={"X-CSRF-Token":self.csrf,"Content-Type":"application/json"}).status_code,413)
        with self.client.get("/static/review.js") as response:
            self.assertNotIn("innerHTML",response.text)
            self.assertIn("textContent",response.text)
        page=self.client.get("/reviews")
        self.assertIn('lang="fa" dir="rtl"',page.text)
        self.assertIn("script-src 'self'",page.headers["Content-Security-Policy"])
        self.assertEqual(page.headers["Cache-Control"],"no-store")

    def test_empty_search_until_explicit_human_edit(self):
        self.assertEqual(self.client.get(self.url+"/search").json["entries"],[])
        self.assertEqual(self.save().status_code,200)
        body=self.client.get(self.url+"/search").json
        self.assertEqual(body["operation"],"replace_document")
        self.assertEqual(len(body["entries"]),1)
        self.assertEqual(body["integration_status"],"NO_CONNECTED_INDEX")

    def test_two_concurrent_writes_cannot_lose_updates(self):
        gate=threading.Barrier(2);outcomes=[]
        def update():
            gate.wait()
            try:
                self.store.save(self.owner,self.doc,1,[{"span_id":"P0001-test","status":"approved","text":"test"}],reviewer="Synthetic tester")
                outcomes.append("saved")
            except ReviewConflict:outcomes.append("conflict")
        threads=[threading.Thread(target=update) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=10)
        self.assertCountEqual(outcomes,["saved","conflict"])
        self.assertEqual(self.store.get(self.owner,self.doc)["version"],2)

    def test_separate_python_processes_share_version_lock(self):
        code = """import sys
from pathlib import Path
from ocr_pipeline.review import ReviewStore,ReviewConflict
store=ReviewStore(Path(sys.argv[1]))
try:
 store.save(sys.argv[2],sys.argv[3],1,[{'span_id':'P0001-test','status':'approved','text':'test'}],reviewer='Synthetic process test')
 print('saved')
except ReviewConflict: print('conflict')
"""
        commands=[sys.executable,"-B","-c",code,str(self.root/"reviews"),self.owner,self.doc]
        processes=[subprocess.Popen(commands,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=os.environ.copy()) for _ in range(2)]
        output=[process.communicate(timeout=30) for process in processes]
        self.assertTrue(all(process.returncode==0 for process in processes),output)
        self.assertCountEqual([out.strip() for out,_ in output],["saved","conflict"])

    @unittest.skipUnless(importlib.util.find_spec("PIL"),"Optional candidate image decoder is not installed")
    def test_candidate_image_upload_is_real_and_legacy_rejects_it(self):
        from PIL import Image
        image=io.BytesIO();Image.new("RGB",(60,40),"white").save(image,format="PNG")
        uploads=self.root/"uploads";uploads.mkdir()
        with patch.object(web,"UPLOAD_DIR",uploads),patch.object(web.threading,"Thread"):
            with patch.object(web,"PIPELINE_MODE","legacy"):
                response=self.client.post('/upload',data={'pdfs':(io.BytesIO(image.getvalue()),'test.png')},headers={'X-CSRF-Token':self.csrf})
                self.assertEqual(response.status_code,400)
            with patch.object(web,"PIPELINE_MODE","candidate"):
                response=self.client.post('/upload',data={'pdfs':(io.BytesIO(image.getvalue()),'test.png'),'profile':'mixed','handwriting':'1'},headers={'X-CSRF-Token':self.csrf})
                self.assertEqual(response.status_code,200)
                job=web.JOBS.pop(response.json['job_id'])
                self.assertTrue(job['candidate_settings']['handwriting'])
                self.assertEqual(job['candidate_settings']['candidate_dpi'],300)
                web.QUEUE_SLOTS.release()

    def test_review_slots_are_bounded(self):
        slots=threading.BoundedSemaphore(1);slots.acquire()
        with patch.object(web,"REVIEW_SLOTS",slots):
            self.assertEqual(self.client.get(self.url).status_code,503)
