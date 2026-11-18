#!/usr/bin/env python3
"""Public changelog for status.aismith.io (updates.json, read by updates.html).

Reads the merged pull requests of the private repositories in CHANGELOG_REPOS
and adds the ones that OPT IN to updates.json, newest first.

Rules, each one fail closed:

- DORMANT without its token. CHANGELOG_TOKEN unset or empty: nothing is read,
  nothing is written, exit 0. No other credential is ever used in its place:
  the only credential this script reads is CHANGELOG_TOKEN, never
  GITHUB_TOKEN, GH_TOKEN or any other secret.
- OPT-IN. A merged pull request is published only when it carries the label
  `public-changelog`; `no-changelog` wins over it; no label, no entry.
  Labels select and are never stored.
- FOUR FIELDS. An entry is merge time, repository name, number and title, as
  written. Nothing else from the private repositories is kept.
- START DATE. Only pull requests merged on or after CHANGELOG_SINCE (UTC,
  YYYY-MM-DD). Unset or malformed: nothing is read, nothing is written.
- INCREMENTAL. Per repository, the pull requests merged on or after the later
  of the start date and that repository's newest stored entry are read. An
  entry's identity is (repository, number); a stored entry is never added
  again. Order: merge time newest first, then repository, then number.
- ALL OR NOTHING. An API error, a rate limit, an unparseable answer or a
  malformed store writes nothing; updates.json is replaced only, atomically,
  when at least one new entry exists, so a run with nothing new changes no
  byte.
- EVERY PAGE. A page is the last only when GitHub's answer says no next page
  exists (its Link header has no rel="next"), whatever the page's length; a
  page that advertises a next page is followed. Pagination that cannot be
  read fails closed.
- QUIET LOGS. This repository and its Actions logs are public, so the log
  carries only the count of entries published, never a title and never a
  count of what was read or skipped.

Exit codes: 0 dormant, nothing new, or written; 1 failed closed; 2 misconfigured.
"""

import calendar
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
OPT_IN = "public-changelog"   # matched exactly
OPT_OUT = "no-changelog"      # matched ignoring case, so it errs toward hiding
PER_PAGE = 100
MAX_PAGES = 20
TIMEOUT_S = 20
KEYS = ("merged_at", "repo", "number", "title")

_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_FULL_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")
_LINK_VALUE = re.compile(r'\s*<([^<>]*)>\s*;\s*rel="([^"]+)"\s*(,|\Z)')


class FailClosed(Exception):
    """Write nothing. The message never carries a title or a credential."""


class Misconfigured(FailClosed):
    pass


def ts_epoch(value, what):
    """Strict UTC timestamp as GitHub writes it, to epoch seconds."""
    if not isinstance(value, str) or not _TS.fullmatch(value):
        raise FailClosed(f"unparseable {what}")
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        raise FailClosed(f"unparseable {what}") from None


def parse_since(value):
    value = (value or "").strip()
    if not _DAY.fullmatch(value):
        raise Misconfigured("CHANGELOG_SINCE unset or not YYYY-MM-DD")
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%d"))
    except ValueError:
        raise Misconfigured("CHANGELOG_SINCE is not a real date") from None


def parse_repos(value):
    repos = (value or "").split()
    if not repos or not all(_FULL_NAME.fullmatch(r) for r in repos):
        raise Misconfigured("CHANGELOG_REPOS unset or not owner/name ...")
    names = [r.split("/", 1)[1] for r in repos]
    if len(set(names)) != len(names):
        raise Misconfigured("CHANGELOG_REPOS names a repository name twice")
    return repos


def load_store(path):
    try:
        with open(path, "rb") as fh:
            store = json.loads(fh.read().decode("utf-8"))
    except FileNotFoundError:
        raise FailClosed(f"{os.path.basename(path)} missing") from None
    except (UnicodeDecodeError, ValueError):
        raise FailClosed(f"{os.path.basename(path)} unparseable") from None
    if not isinstance(store, list):
        raise FailClosed(f"{os.path.basename(path)} is not a list")
    seen = set()
    for e in store:
        if not isinstance(e, dict) or set(e) != set(KEYS):
            raise FailClosed("stored entry malformed")
        ts_epoch(e["merged_at"], "stored merge time")
        if not isinstance(e["repo"], str) or not isinstance(e["title"], str):
            raise FailClosed("stored entry malformed")
        if type(e["number"]) is not int or e["number"] < 1:
            raise FailClosed("stored entry malformed")
        ident = (e["repo"], e["number"])
        if ident in seen:
            raise FailClosed("stored entry duplicated")
        seen.add(ident)
    return store


def order_key(e):
    return (-ts_epoch(e["merged_at"], "merge time"), e["repo"], e["number"])


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only within the API's own scheme and host, so the
    token is never sent anywhere else (a renamed or moved repository answers
    with a redirect inside the API)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise FailClosed(f"redirect off the API host refused (HTTP {code})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get_json(url, token):
    """GET url; return (decoded JSON, Link header or None). The Link header is
    GitHub's pagination metadata and is kept for next_page() to read."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "aismith-status-changelog",
    })
    opener = urllib.request.build_opener(_SameHostRedirect)
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            status, body = resp.status, resp.read()
            links = resp.headers.get_all("Link") or []
    except urllib.error.HTTPError as e:
        hdrs = e.headers or {}
        limited = e.code in (403, 429) and (
            hdrs.get("x-ratelimit-remaining") == "0" or bool(hdrs.get("retry-after")))
        raise FailClosed(f"HTTP {e.code}{' (rate limited)' if limited else ''}") from None
    except FailClosed:
        raise
    except (urllib.error.URLError, OSError) as e:
        raise FailClosed(f"network error ({type(e).__name__})") from None
    if status != 200:
        raise FailClosed(f"HTTP {status}")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FailClosed("unparseable answer") from None
    return data, (", ".join(links) if links else None)


def next_page(link, page):
    """Read GitHub's pagination metadata for the answer to `page`: the number
    of the next page, or None when the answer says no next page exists.
    GitHub sends no Link header when one page holds everything, and a Link
    header (at least rel="prev") on every later page. Anything else is
    unreadable and fails closed: a malformed header, a later page without one,
    or a next page other than page + 1."""
    if link is None:
        if page == 1:
            return None
        raise FailClosed("unreadable pagination")
    nexts, pos = [], 0
    while True:
        m = _LINK_VALUE.match(link, pos)
        if not m:
            raise FailClosed("unreadable pagination")
        if "next" in m.group(2).split():
            nexts.append(m.group(1))
        if not m.group(3):
            break
        pos = m.end()
    if not nexts:
        return None
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(nexts[0]).query)
    except ValueError:
        raise FailClosed("unreadable pagination") from None
    if len(nexts) != 1 or query.get("page") != [str(page + 1)]:
        raise FailClosed("unreadable pagination")
    return page + 1


def _read_pr(pr):
    """Validate one API item; return what selection needs. Anything off-shape
    fails the whole run rather than being guessed at."""
    if not isinstance(pr, dict):
        raise FailClosed("unparseable pull request")
    number, title, labels = pr.get("number"), pr.get("title"), pr.get("labels")
    if type(number) is not int or number < 1 or not isinstance(title, str):
        raise FailClosed("unparseable pull request")
    if not isinstance(labels, list) or not all(
            isinstance(l, dict) and isinstance(l.get("name"), str) for l in labels):
        raise FailClosed("unparseable pull request labels")
    merged_at = pr.get("merged_at")
    merged = None if merged_at is None else ts_epoch(merged_at, "merge time")
    updated = ts_epoch(pr.get("updated_at"), "update time")
    return number, title, merged_at, merged, updated, [l["name"] for l in labels]


def merged_since(get, token, full_name, cutoff):
    """Every pull request of full_name merged at or after cutoff, all pages or
    none. Sorted by update time, newest first: merging updates a pull request,
    so once an item's update time is before the cutoff, nothing after it can
    have been merged at or after the cutoff. Otherwise a page is the last only
    when its answer says no next page exists (next_page()), never because it
    holds fewer than PER_PAGE items."""
    found, numbers = [], set()
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode({"state": "closed", "sort": "updated",
                                        "direction": "desc", "per_page": PER_PAGE,
                                        "page": page})
        data, link = get(f"{API}/repos/{full_name}/pulls?{query}", token)
        if not isinstance(data, list):
            raise FailClosed("unparseable answer")
        following = next_page(link, page)
        for pr in data:
            number, title, merged_at, merged, updated, labels = _read_pr(pr)
            if updated < cutoff:
                return found
            if merged is None or merged < cutoff or number in numbers:
                continue
            numbers.add(number)
            found.append((number, title, merged_at, labels))
        if following is None:
            return found
    raise FailClosed(f"more than {MAX_PAGES} pages")


def eligible(labels):
    if any(name.lower() == OPT_OUT for name in labels):
        return False
    return OPT_IN in labels


def run(path, env, get=http_get_json, out=print):
    token = (env.get("CHANGELOG_TOKEN") or "").strip()
    if not token:
        out("changelog: dormant, CHANGELOG_TOKEN unset; nothing read, nothing written")
        return 0
    since = parse_since(env.get("CHANGELOG_SINCE"))
    repos = parse_repos(env.get("CHANGELOG_REPOS"))
    store = load_store(path)
    stored = {(e["repo"], e["number"]) for e in store}

    new = []
    for full_name in repos:
        name = full_name.split("/", 1)[1]
        newest = max((ts_epoch(e["merged_at"], "stored merge time")
                      for e in store if e["repo"] == name), default=since)
        cutoff = max(since, newest)
        prs = merged_since(get, token, full_name, cutoff)
        picked = [p for p in prs if eligible(p[3]) and (name, p[0]) not in stored]
        out(f"changelog: {name}: {len(picked)} new")
        new += [dict(zip(KEYS, (merged_at, name, number, title)))
                for number, title, merged_at, _labels in picked]

    if not new:
        out("changelog: nothing new; updates.json untouched")
        return 0
    entries = sorted(store + new, key=order_key)
    text = json.dumps(entries, ensure_ascii=False, indent=2) + "\n"
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".updates-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    out(f"changelog: {len(new)} new entries written, {len(entries)} in all")
    return 0


def main(argv):
    path = argv[1] if len(argv) > 1 else "updates.json"
    try:
        return run(path, os.environ)
    except Misconfigured as e:
        print(f"changelog: misconfigured, nothing written: {e}", file=sys.stderr)
        return 2
    except FailClosed as e:
        print(f"changelog: failed closed, nothing written: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
