from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import os
import httpx
import time

from fastapi.testclient import TestClient

import app


class CalendarTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.origin = "https://facilpost.example"
        env = patch.dict(os.environ, STATE_DIR=directory.name, PUBLIC_ORIGIN=self.origin,
                         META_APP_SECRET="test-secret", META_APP_ID="123")
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app.app, base_url=self.origin, follow_redirects=False,
                                 headers={"origin":self.origin})
        self.addCleanup(self.client.close)

    def login(self):
        start = self.client.get("/api/facebook/connect")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        pages = [{"id":"123","name":"Página","tasks":["CREATE_CONTENT"],"access_token":"page-token"}]
        with patch.object(app,"graph",side_effect=[
                {"access_token":"short"}, {"access_token":"long"}, {"data":pages}]):
            callback = self.client.get("/api/facebook/callback", params={"state":state,"code":"code"})
        self.assertEqual(callback.headers["location"], "/")
        return state

    def test_graph_boolean_mutation_response(self):
        response = httpx.Response(200,json=True)
        with patch.object(app.httpx,"request",return_value=response):
            self.assertEqual(app.graph("DELETE","123_9","page-token"), {"success":True})

    def test_login_for_business_configuration(self):
        with patch.dict(os.environ,META_LOGIN_CONFIG_ID="989"):
            options=parse_qs(urlsplit(self.client.get("/api/facebook/connect").headers["location"]).query)
        self.assertEqual(options["config_id"],["989"])
        self.assertNotIn("scope",options)

    def test_login_and_logout(self):
        state = self.login()
        result = self.client.get("/api/session").json()
        self.assertEqual(result,{"connected":True,"pages":[{"id":"123","name":"Página"}],"page_id":"123"})
        with app.database() as db:
            token = db.execute("SELECT token FROM pages").fetchone()[0]
            self.assertNotIn(b"page-token",token)
        replay = self.client.get("/api/facebook/callback",params={"state":state,"code":"code"})
        self.assertIn("error=",replay.headers["location"])
        self.assertEqual(self.client.post("/api/page",json={"id":"456"}).status_code,403)
        self.assertEqual(self.client.post("/api/logout",headers={"origin":"https://example.org"}).status_code,403)
        self.assertEqual(self.client.post("/api/logout").status_code,200)
        self.assertFalse(self.client.get("/api/session").json()["connected"])

    def test_calendar_multiple_pages_and_month_boundary(self):
        self.login()
        raw = {"id":"123_3","created_time":"2026-11-01T03:00:00+00:00","message":"Vídeo",
               "attachments":{"data":[{"media_type":"video","media":{"source":"https://fb.example/clip",
                                       "image":{"src":"https://fb.example/frame"}}}]}}
        pending = {"id":"123_2","scheduled_publish_time":int(datetime(2026,11,2,tzinfo=timezone.utc).timestamp())}
        old = {**raw, "id":"123_1","created_time":"2026-11-01T02:59:59+00:00"}
        with patch.object(app,"graph",side_effect=[{"data":[pending]}, {"data":[raw],"paging":{
            "next":"https://graph.facebook.com/v27.0/123/published_posts?after=NEXT"}}, {"data":[old]}]) as g:
            res = self.client.get("/api/posts",params={"start":"2026-11-01T00:00:00-03:00",
                                                      "end":"2026-12-01T00:00:00-03:00"})
        self.assertEqual(res.status_code,200,res.text)
        rows=res.json()["posts"]
        self.assertEqual([p["id"] for p in rows],["123_3","123_2"])
        self.assertEqual(rows[0]["media_url"],"https://fb.example/clip")
        self.assertEqual(rows[1]["status"],"scheduled")
        self.assertEqual(g.call_args.kwargs["params"]["after"],"NEXT")

    def test_publish_schedule_media_edit_delete_and_metrics(self):
        self.login()
        future=(datetime.now(timezone.utc)+timedelta(hours=3)).isoformat()
        with patch.object(app,"graph",return_value={"id":"123_9"}) as g:
            self.assertEqual(self.client.post("/api/posts",data={"message":"Olá"}).status_code,201)
            self.assertEqual(g.call_args.args,("POST","123/feed","page-token"))
            self.assertEqual(self.client.post("/api/posts",data={"at":future},
                files={"file":("foto.png",b"data","image/png")}).status_code,201)
            self.assertEqual(g.call_args.args[1],"123/photos")
            self.assertEqual(g.call_args.kwargs["data"]["published"],"false")
            self.assertEqual(self.client.post("/api/posts",
                files={"file":("segmento.mp4",b"data","video/mp4")}).status_code,201)
            self.assertTrue(g.call_args.kwargs["video"])
            self.assertEqual(self.client.patch("/api/posts/123_9",json={"message":"Editado","at":future}).status_code,200)
            self.assertEqual(self.client.delete("/api/posts/456_9").status_code,404)
            self.assertEqual(self.client.delete("/api/posts/123_9").status_code,200)
        with patch.object(app,"graph",return_value={"reactions":{"summary":{"total_count":7}}}):
            self.assertEqual(self.client.get("/api/posts/123_9/metrics").json(),
                             {"reactions":7,"comments":None,"shares":None})
        past=datetime.fromtimestamp(time.time()-100, timezone.utc).isoformat()
        self.assertEqual(self.client.post("/api/posts",data={"at":past,"message":"X"}).status_code,422)


if __name__ == "__main__":
    main()
