"""Tests for scripts/changelog.py against recorded fixtures (invented titles).

Run from the repository root:  python3 -m unittest discover -s tests -v

Standard library only. No test reaches the network: fixture runs use a fake
transport, and the transport tests use a local HTTP server on 127.0.0.1.
"""

import copy
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
SCRIPT = os.path.join(ROOT, "scripts", "changelog.py")

spec = importlib.util.spec_from_file_location("changelog", SCRIPT)
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)

SEED, PACKS = "AISmithFactory/aismith-hub-seed", "AISmithFactory/aismith-packs"
TOKEN = "fixture-token-not-a-real-credential"
ENV = {"CHANGELOG_TOKEN": TOKEN, "CHANGELOG_SINCE": "2026-11-20",
       "CHANGELOG_REPOS": f"{SEED} {PACKS}"}


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


class FakeAPI:
    """Answers GET /repos/<owner>/<name>/pulls?...&page=N from recorded pages."""

    def __init__(self, pages):
        self.pages = pages          # {full_name: [page1, page2, ...]}
        self.calls = []

    def __call__(self, url, token):
        self.calls.append((url, token))
        parts = urllib.parse.urlsplit(url)
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls", parts.path)
        q = urllib.parse.parse_qs(parts.query)
        assert m and q["state"] == ["closed"] and q["sort"] == ["updated"], url
        assert q["direction"] == ["desc"], url
        pages = self.pages[m.group(1)]
        page = int(q["page"][0])
        if isinstance(pages, Exception):
            raise pages
        return copy.deepcopy(pages[page - 1]) if page <= len(pages) else []


class Workdir:
    """A disposable updates.json, with its bytes and mtime recorded."""

    def __init__(self, test, store_fixture=None, text="[]\n"):
        self.dir = tempfile.mkdtemp(prefix="changelog-test-")
        test.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "updates.json")
        if store_fixture:
            shutil.copyfile(os.path.join(FIX, store_fixture), self.path)
        elif text is not None:
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(text)
        self.before = self.snapshot()

    def snapshot(self):
        if not os.path.exists(self.path):
            return None
        with open(self.path, "rb") as fh:
            return fh.read(), os.stat(self.path).st_mtime_ns, sorted(os.listdir(self.dir))

    def untouched(self):
        return self.snapshot() == self.before

    def entries(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)


def run(work, env, api):
    lines = []
    code = cl.run(work.path, env, get=api, out=lines.append)
    return code, lines


def idents(entries):
    return [(e["repo"], e["number"]) for e in entries]


class Selection(unittest.TestCase):
    """The main recorded run: both repositories, start date 2026-11-20."""

    def setUp(self):
        self.work = Workdir(self)
        self.api = FakeAPI({SEED: [fixture("pulls-hub-seed.json")],
                            PACKS: [fixture("pulls-packs.json")]})
        self.code, self.lines = run(self.work, ENV, self.api)
        self.entries = self.work.entries()
        self.ids = idents(self.entries)

    def test_run_succeeds_with_the_dedicated_token_only(self):
        self.assertEqual(self.code, 0)
        self.assertTrue(self.api.calls)
        self.assertTrue(all(tok == TOKEN for _url, tok in self.api.calls))

    def test_merged_pr_with_public_changelog_is_listed(self):
        self.assertIn(("aismith-hub-seed", 301), self.ids)
        self.assertIn(("aismith-packs", 120), self.ids)

    def test_unmerged_pr_is_absent(self):
        self.assertNotIn(("aismith-hub-seed", 300), self.ids)

    def test_no_changelog_pr_is_absent(self):
        self.assertNotIn(("aismith-hub-seed", 298), self.ids)

    def test_pr_with_both_labels_is_absent(self):
        self.assertNotIn(("aismith-hub-seed", 299), self.ids)
        self.assertNotIn(("aismith-packs", 118), self.ids)   # opt-out in another case

    def test_merged_pr_with_no_label_is_absent(self):
        self.assertNotIn(("aismith-hub-seed", 297), self.ids)

    def test_opt_in_label_must_match_exactly(self):
        self.assertNotIn(("aismith-packs", 119), self.ids)

    def test_merged_before_start_date_is_absent(self):
        self.assertNotIn(("aismith-hub-seed", 295), self.ids)   # 23:59:59 the day before
        self.assertNotIn(("aismith-hub-seed", 294), self.ids)
        self.assertIn(("aismith-packs", 117), self.ids)         # 00:00:00 on the day

    def test_reading_stops_at_the_first_pr_updated_before_the_window(self):
        # Past #294 the recorded page holds a malformed item; a run that kept
        # reading would have failed closed instead of succeeding.
        self.assertEqual(self.code, 0)

    def test_newest_first_ties_by_repository_then_number(self):
        self.assertEqual(self.ids, [("aismith-hub-seed", 301), ("aismith-packs", 120),
                                    ("aismith-hub-seed", 296), ("aismith-packs", 117)])

    def test_only_the_four_fields_leave(self):
        for e in self.entries:
            self.assertEqual(list(e), ["merged_at", "repo", "number", "title"])
        with open(self.work.path, encoding="utf-8") as fh:
            raw = fh.read()
        for leaked in ("SENTINEL", "sentinel", "github.com", "api.github",
                       "5e5e5e5e", "abababab", "public-changelog", "no-changelog",
                       "AISmithFactory/", "2026-11-01T08", "closed"):
            self.assertNotIn(leaked, raw)

    def test_title_with_markup_is_stored_as_written(self):
        e = next(e for e in self.entries if e["number"] == 296)
        self.assertEqual(e["title"], 'Show <script>alert("x")</script> & <b>bold</b> '
                                     '<img src=x onerror=alert(1)> as text')

    def test_log_carries_no_title_and_no_read_count(self):
        log = "\n".join(self.lines)
        for pr in fixture("pulls-hub-seed.json") + fixture("pulls-packs.json"):
            if isinstance(pr.get("title"), str):
                self.assertNotIn(pr["title"], log)
        self.assertEqual(self.lines, ["changelog: aismith-hub-seed: 2 new",
                                      "changelog: aismith-packs: 2 new",
                                      "changelog: 4 new entries written, 4 in all"])


class Idempotence(unittest.TestCase):
    """The attack's case: the store holds the newest eligible pair; the API
    returns it and one newer; only the newer is added; the same run again
    changes no byte."""

    ENV = dict(ENV, CHANGELOG_REPOS=SEED)

    def test_only_the_newer_is_prepended_and_a_rerun_changes_no_byte(self):
        work = Workdir(self, store_fixture="idem-store.json")
        api = FakeAPI({SEED: [fixture("idem-pulls-hub-seed.json")]})
        code, _ = run(work, self.ENV, api)
        self.assertEqual(code, 0)
        self.assertEqual(idents(work.entries()), [("aismith-hub-seed", 302),
                                                  ("aismith-hub-seed", 301)])
        self.assertEqual(work.entries()[1], fixture("idem-store.json")[0])
        # The window starts at the stored entry's own merge time, inclusive.
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(api.calls[0][0]).query)
        self.assertEqual(q["page"], ["1"])

        after_first = work.snapshot()
        code, lines = run(work, self.ENV, api)
        self.assertEqual(code, 0)
        self.assertEqual(work.snapshot(), after_first)
        self.assertEqual(lines[-1], "changelog: nothing new; updates.json untouched")

    def test_stored_entry_is_never_added_again_even_with_a_new_title(self):
        work = Workdir(self, store_fixture="idem-store.json")
        page = fixture("idem-pulls-hub-seed.json")[1:]          # #301 and #300 only
        page[0]["title"] = "Retitled after merge"
        code, lines = run(work, self.ENV, FakeAPI({SEED: [page]}))
        self.assertEqual(code, 0)
        self.assertTrue(work.untouched())

    def test_whole_run_twice_on_the_main_fixtures_is_byte_identical(self):
        work = Workdir(self)
        api = FakeAPI({SEED: [fixture("pulls-hub-seed.json")],
                       PACKS: [fixture("pulls-packs.json")]})
        run(work, ENV, api)
        first = work.snapshot()
        run(work, ENV, api)
        self.assertEqual(work.snapshot(), first)


class Dormant(unittest.TestCase):
    """No token: nothing read, nothing written, exit 0, whatever else is set."""

    def assert_dormant(self, env):
        work = Workdir(self)
        api = FakeAPI({SEED: cl.FailClosed("must not be called"),
                       PACKS: cl.FailClosed("must not be called")})
        code, lines = run(work, env, api)
        self.assertEqual(code, 0)
        self.assertEqual(api.calls, [])
        self.assertTrue(work.untouched())
        self.assertIn("dormant", lines[0])

    def test_token_unset_with_other_credentials_present(self):
        env = dict(ENV, GITHUB_TOKEN="other-1", GH_TOKEN="other-2", GITHUB_PAT="other-3")
        del env["CHANGELOG_TOKEN"]
        self.assert_dormant(env)

    def test_token_empty(self):
        self.assert_dormant(dict(ENV, CHANGELOG_TOKEN="", GITHUB_TOKEN="other-1"))

    def test_token_blank(self):
        self.assert_dormant(dict(ENV, CHANGELOG_TOKEN="  \n"))

    def test_dormant_even_with_no_store_and_no_settings(self):
        work = Workdir(self, text=None)
        code, _ = run(work, {}, FakeAPI({}))
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(work.path))

    def test_cli_end_to_end_exits_0_and_writes_nothing(self):
        work = Workdir(self)
        env = {k: v for k, v in os.environ.items() if not k.startswith("CHANGELOG_")}
        env.update(GITHUB_TOKEN="other-1", GH_TOKEN="other-2",
                   CHANGELOG_REPOS=ENV["CHANGELOG_REPOS"], CHANGELOG_SINCE="2026-11-20",
                   HTTPS_PROXY="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9")
        res = subprocess.run([sys.executable, "-I", SCRIPT, work.path], env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("dormant", res.stdout)
        self.assertTrue(work.untouched())

    def test_script_reads_no_credential_but_its_own(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(sorted(set(re.findall(r"env\.get\(\"([A-Z_]+)\"\)", src))),
                         ["CHANGELOG_REPOS", "CHANGELOG_SINCE", "CHANGELOG_TOKEN"])
        self.assertEqual(len(re.findall(r"os\.environ", src)), 1)   # passed to run() once
        self.assertNotIn("getenv", src)


class FailsClosed(unittest.TestCase):
    """An error anywhere writes nothing: updates.json keeps every byte."""

    def assert_untouched(self, api, env=ENV, store_fixture=None, code=1):
        work = Workdir(self, store_fixture=store_fixture)
        with self.assertRaises(cl.FailClosed) as ctx:
            run(work, env, api)
        self.assertTrue(work.untouched())
        self.assertEqual(code, 2 if isinstance(ctx.exception, cl.Misconfigured) else 1)
        return ctx.exception

    def test_api_error_on_the_second_repository_after_the_first_succeeded(self):
        # The first repository yields new entries; the second fails; none is written.
        self.assert_untouched(FakeAPI({SEED: [fixture("pulls-hub-seed.json")],
                                       PACKS: cl.FailClosed("HTTP 502")}))

    def test_api_error_leaves_a_populated_store_as_it_was(self):
        self.assert_untouched(FakeAPI({SEED: [fixture("idem-pulls-hub-seed.json")],
                                       PACKS: cl.FailClosed("HTTP 502")}),
                              store_fixture="idem-store.json")

    def test_answer_that_is_not_a_list(self):
        self.assert_untouched(FakeAPI({SEED: [{"message": "Bad credentials"}],
                                       PACKS: [[]]}))

    def test_malformed_item(self):
        page = fixture("pulls-hub-seed.json")
        page[0]["labels"] = "public-changelog"
        self.assert_untouched(FakeAPI({SEED: [page], PACKS: [[]]}))

    def test_malformed_merge_time(self):
        page = fixture("pulls-hub-seed.json")
        page[0]["merged_at"] = "25 November 2026"
        self.assert_untouched(FakeAPI({SEED: [page], PACKS: [[]]}))

    def test_partial_pages_are_never_written(self):
        old = cl.PER_PAGE
        cl.PER_PAGE = 2
        self.addCleanup(setattr, cl, "PER_PAGE", old)
        page1 = fixture("pulls-hub-seed.json")[:1] + fixture("pulls-hub-seed.json")[5:6]
        # page 1 is full (two eligible entries), page 2 errors
        self.assert_untouched(FakeAPI({SEED: _PagesThenError([page1]), PACKS: [[]]}))

    def test_too_many_pages(self):
        old_pp, old_max = cl.PER_PAGE, cl.MAX_PAGES
        cl.PER_PAGE, cl.MAX_PAGES = 1, 2
        self.addCleanup(setattr, cl, "PER_PAGE", old_pp)
        self.addCleanup(setattr, cl, "MAX_PAGES", old_max)
        pages = [[p] for p in fixture("pulls-hub-seed.json")[:3]]
        self.assert_untouched(FakeAPI({SEED: pages, PACKS: [[]]}))

    def test_start_date_unset_reads_nothing(self):
        api = FakeAPI({})
        env = dict(ENV, CHANGELOG_SINCE="")
        self.assert_untouched(api, env=env, code=2)
        self.assertEqual(api.calls, [])

    def test_start_date_malformed(self):
        for bad in ("20 November 2026", "2026-02-30", "2026-11-20T00:00:00Z"):
            with self.subTest(bad=bad):
                self.assert_untouched(FakeAPI({}), env=dict(ENV, CHANGELOG_SINCE=bad), code=2)

    def test_repository_list_malformed(self):
        for bad in ("", "aismith-hub-seed", f"{SEED} Other/aismith-hub-seed"):
            with self.subTest(bad=bad):
                self.assert_untouched(FakeAPI({}), env=dict(ENV, CHANGELOG_REPOS=bad), code=2)

    def test_store_unparseable_or_malformed(self):
        for text in ("{not json", '{"entries": []}', '[{"repo": "x"}]',
                     '[{"merged_at": "2026-11-25T10:00:00Z", "repo": "aismith-hub-seed", '
                     '"number": 1, "title": "t", "author": "someone"}]'):
            with self.subTest(text=text):
                work = Workdir(self, text=text)
                api = FakeAPI({})
                with self.assertRaises(cl.FailClosed):
                    run(work, ENV, api)
                self.assertTrue(work.untouched())
                self.assertEqual(api.calls, [])

    def test_store_missing(self):
        work = Workdir(self, text=None)
        with self.assertRaises(cl.FailClosed):
            run(work, ENV, FakeAPI({}))
        self.assertIsNone(work.snapshot())


class _PagesThenError:
    """Serves the given pages, then fails on the next one."""

    def __init__(self, pages):
        self.pages = pages

    def __len__(self):
        return len(self.pages) + 1

    def __getitem__(self, i):
        if i < len(self.pages):
            return self.pages[i]
        raise cl.FailClosed("HTTP 500 on a later page")


class _Server(http.server.BaseHTTPRequestHandler):
    routes = {}
    seen_auth = []

    def do_GET(self):
        _Server.seen_auth.append(self.headers.get("Authorization"))
        status, headers, body = _Server.routes.get(urllib.parse.urlsplit(self.path).path,
                                                   (404, {}, b"{}"))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Transport(unittest.TestCase):
    """The real HTTP transport against a local server: each error writes nothing."""

    @classmethod
    def setUpClass(cls):
        cls.saved = {k: os.environ.get(k) for k in ("no_proxy", "NO_PROXY")}
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Server)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.old_api = cl.API
        cl.API = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cl.API = cls.old_api
        for k, v in cls.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        _Server.routes = {f"/repos/{PACKS}/pulls": (200, {}, b"[]")}
        _Server.seen_auth = []

    def route_seed(self, status, headers, body):
        _Server.routes[f"/repos/{SEED}/pulls"] = (status, headers, body)

    def run_real(self):
        work = Workdir(self, store_fixture="idem-store.json")
        try:
            code, lines = run(work, ENV, cl.http_get_json)
        except cl.FailClosed as e:
            return work, None, str(e)
        return work, code, lines

    def test_success_through_the_real_transport(self):
        self.route_seed(200, {}, json.dumps(fixture("idem-pulls-hub-seed.json")).encode())
        work, code, _ = self.run_real()
        self.assertEqual(code, 0)
        self.assertEqual(idents(work.entries())[0], ("aismith-hub-seed", 302))
        self.assertEqual(set(_Server.seen_auth), {f"Bearer {TOKEN}"})

    def test_http_500(self):
        self.route_seed(500, {}, b'{"message": "Server Error"}')
        work, code, err = self.run_real()
        self.assertIsNone(code)
        self.assertEqual(err, "HTTP 500")
        self.assertTrue(work.untouched())

    def test_rate_limit(self):
        self.route_seed(403, {"x-ratelimit-remaining": "0"}, b'{"message": "rate limit"}')
        work, code, err = self.run_real()
        self.assertEqual(err, "HTTP 403 (rate limited)")
        self.assertTrue(work.untouched())

    def test_secondary_rate_limit_429(self):
        self.route_seed(429, {"retry-after": "60"}, b"{}")
        work, code, err = self.run_real()
        self.assertEqual(err, "HTTP 429 (rate limited)")
        self.assertTrue(work.untouched())

    def test_unparseable_answer(self):
        self.route_seed(200, {}, b"<html>not json</html>")
        work, code, err = self.run_real()
        self.assertEqual(err, "unparseable answer")
        self.assertTrue(work.untouched())

    def test_redirect_off_the_api_host_is_refused(self):
        self.route_seed(301, {"Location": f"http://localhost:{self.port}/elsewhere"}, b"")
        _Server.routes["/elsewhere"] = (200, {}, b"[]")
        work, code, err = self.run_real()
        self.assertIn("redirect off the API host refused", err)
        self.assertTrue(work.untouched())
        self.assertEqual(len(_Server.seen_auth), 1)    # the token went nowhere else

    def test_network_error(self):
        old = cl.API
        cl.API = "http://127.0.0.1:9"
        self.addCleanup(setattr, cl, "API", old)
        work, code, err = self.run_real()
        self.assertTrue(err.startswith("network error"))
        self.assertTrue(work.untouched())


class Surfaces(unittest.TestCase):
    """The page shows titles as text, and the workflow passes one secret only."""

    def read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_page_writes_every_value_as_text(self):
        page = self.read("updates.html")
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("outerHTML", page)
        self.assertNotIn("insertAdjacentHTML", page)
        self.assertNotIn("document.write", page)
        self.assertIn("title.textContent = u.title", page)
        self.assertEqual(re.findall(r"<a href=\"([^\"]*)\"", page), ["./"])   # no entry links
        self.assertIn("No updates yet.", page)

    def test_shipped_store_is_empty(self):
        self.assertEqual(self.read("updates.json"), "[]\n")

    def test_workflow_passes_only_the_changelog_secret_to_the_changelog_step(self):
        wf = self.read(".github/workflows/update-status.yml")
        self.assertEqual(re.findall(r"secrets\.[A-Za-z_]+", wf), ["secrets.CHANGELOG_READ_TOKEN"])
        self.assertNotIn("GITHUB_TOKEN", wf)
        step = wf[wf.index("- name: Changelog"):]
        self.assertIn("continue-on-error: true", step[:200])
        self.assertLess(wf.index("- name: Commit if changed (ignoring the timestamp)"),
                        wf.index("- name: Changelog"))
        self.assertRegex(wf, r'\n {10}CHANGELOG_SINCE: "[0-9-]*"\n')


if __name__ == "__main__":
    unittest.main()
