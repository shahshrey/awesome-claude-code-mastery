#!/usr/bin/env python3
"""Deep research agent for awesome-claude-code-mastery.

It does two jobs:

1. discover: run many Tavily searches about Claude Code, collect every GitHub
   repo the results mention, check each repo with the GitHub API, and keep the
   ones that pass the list rules (official, or 100+ stars and active).
2. stale: check every GitHub repo already in README.MD and list the ones that
   are archived, missing, or have no commit in the last N days.

Only the Python standard library is used.

Environment:
  TAVILY_API_KEY  required for discover
  GITHUB_TOKEN    recommended (falls back to `gh auth token`)

Examples:
  python3 scripts/research_agent.py discover --out research/candidates.md
  python3 scripts/research_agent.py stale --days 90 --out research/stale.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.MD")
TAVILY_URL = "https://api.tavily.com/search"
GITHUB_GRAPHQL = "https://api.github.com/graphql"
OFFICIAL_OWNERS = {"anthropics", "modelcontextprotocol"}
MIN_STARS = 100
REPO_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
SKIP_OWNERS = {
    "orgs", "topics", "sponsors", "features", "marketplace", "apps", "settings",
    "login", "about", "pricing", "collections", "trending", "search", "users",
    "enterprise", "customer-stories", "readme", "site", "security", "events",
}

# Each query targets one section of the list. Add more as the list grows.
QUERIES = {
    "official": [
        "Anthropic Claude Code new feature announcement",
        "Claude Code changelog latest release",
        "site:anthropic.com Claude Code",
        "site:code.claude.com docs new",
    ],
    "skills": [
        "Claude Code skills GitHub repository",
        "Agent Skills SKILL.md collection GitHub",
        "best Claude Code skills new",
    ],
    "plugins": [
        "Claude Code plugin marketplace GitHub",
        "Claude Code plugins new release",
    ],
    "mods": [
        "Claude Code mods TypeScript hooks examples",
    ],
    "subagents": [
        "Claude Code subagents collection GitHub",
        "Claude Code agent teams orchestration tool",
    ],
    "multi-session": [
        "run multiple Claude Code sessions parallel git worktrees tool",
        "Claude Code orchestrator desktop app open source",
    ],
    "hooks": [
        "Claude Code hooks examples GitHub",
        "Claude Code scheduled tasks loop routines",
    ],
    "headless-ci": [
        "Claude Code GitHub Actions headless CI",
        "claude -p automation pipeline open source",
    ],
    "mcp": [
        "new MCP server for Claude Code GitHub",
        "Model Context Protocol server popular 2026",
    ],
    "sdk": [
        "Claude Agent SDK example project GitHub",
        "Claude Agent SDK tutorial",
    ],
    "memory": [
        "Claude Code memory context tool GitHub",
    ],
    "gui": [
        "Claude Code GUI desktop app open source",
        "Claude Code mobile remote control app",
    ],
    "utilities": [
        "Claude Code statusline usage monitor tool",
        "Claude Code CLI utility open source",
    ],
    "learning": [
        "Claude Code tutorial guide 2026",
        "Claude Code best practices article",
        "Claude Code course video",
    ],
    "alternatives": [
        "open source alternative to Claude Code coding agent",
    ],
}


# Run once per month in the window, so every month gets its own results.
MONTHLY_QUERIES = [
    "Claude Code new tool GitHub {month}",
    "Claude Code plugin OR skill OR hook released {month}",
    "Claude Code update features {month}",
]


def months_since(since: str) -> list[str]:
    d = dt.date.fromisoformat(since).replace(day=1)
    today = dt.date.today()
    out = []
    while d <= today:
        out.append(d.strftime("%B %Y"))
        d = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return out


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def http_json(url: str, payload: dict, headers: dict, retries: int = 3) -> dict:
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "awesome-claude-code-research-agent")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt * 3)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 3)
                continue
            raise
    return {}


def github_token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except Exception:
        sys.exit("Set GITHUB_TOKEN or log in with `gh auth login`.")


def normalize(owner: str, name: str) -> str | None:
    name = re.sub(r"\.git$", "", name).rstrip(".")
    if owner.lower() in SKIP_OWNERS or not name:
        return None
    return f"{owner}/{name}"


def repos_in(text: str) -> set[str]:
    out = set()
    for owner, name in REPO_RE.findall(text):
        r = normalize(owner, name)
        if r:
            out.add(r)
    return out


def readme_repos() -> set[str]:
    with open(README, encoding="utf-8") as f:
        return {r.lower() for r in repos_in(f.read())}


def readme_urls() -> set[str]:
    with open(README, encoding="utf-8") as f:
        return {u.rstrip("/").lower() for u in re.findall(r"\((https?://[^)\s]+)\)", f.read())}


# ---------------------------------------------------------------- Tavily


def tavily_search(query: str, key: str, since: str) -> list[dict]:
    payload = {
        "query": query,
        "search_depth": "advanced",
        "topic": "general",
        "start_date": since,
        "max_results": 20,
        "include_raw_content": False,
    }
    try:
        res = http_json(TAVILY_URL, payload, {"Authorization": f"Bearer {key}"})
    except Exception as e:  # keep going on one bad query
        log(f"  tavily error for {query!r}: {e}")
        return []
    return res.get("results", [])


# ---------------------------------------------------------------- GitHub


GITHUB_SEARCH_QUERIES = [
    "topic:claude-code", "topic:claude-code-plugin", "topic:claude-code-skills",
    "topic:claude-code-mods", "topic:claude-code-hooks", "topic:claude-code-subagents",
    "claude-code in:name", "\"claude code\" in:description",
]
STAR_BANDS = ["100..149", "150..199", "200..299", "300..499", "500..799",
              "800..1499", "1500..2999", "3000..9999", ">=10000"]


def github_search_sweep(since: str, token: str) -> set[str]:
    """Find repos created since `since` with 100+ stars through the GitHub search API.

    Star bands keep each query under the 1,000-result cap. The search API allows
    30 requests a minute, so the sweep sleeps between calls.
    """
    found = set()
    for q in GITHUB_SEARCH_QUERIES:
        for band in STAR_BANDS:
            for page in range(1, 11):
                params = urllib.parse.urlencode({
                    "q": f"{q} created:>={since} stars:{band} archived:false fork:false",
                    "per_page": 100, "page": page})
                req = urllib.request.Request(f"https://api.github.com/search/repositories?{params}")
                req.add_header("Authorization", f"bearer {token}")
                req.add_header("User-Agent", "awesome-claude-code-research-agent")
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        items = json.loads(resp.read().decode()).get("items", [])
                except urllib.error.HTTPError as e:
                    log(f"  github search error ({e.code}) for {q} {band}; waiting")
                    time.sleep(60)
                    continue
                found.update(it["full_name"] for it in items)
                time.sleep(2.2)
                if len(items) < 100:
                    break
    return found


REPO_FIELDS = (
    "nameWithOwner stargazerCount pushedAt isArchived isFork description url "
    "defaultBranchRef{target{...on Commit{committedDate}}} "
    "repositoryTopics(first:20){nodes{topic{name}}} "
    "readme:object(expression:\"HEAD:README.md\"){...on Blob{text}}"
)


def github_meta(repos: list[str], token: str) -> dict[str, dict | None]:
    out: dict[str, dict | None] = {}
    for i in range(0, len(repos), 50):
        chunk = repos[i : i + 50]
        parts = []
        for j, r in enumerate(chunk):
            owner, name = r.split("/", 1)
            parts.append(f'r{j}:repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{{REPO_FIELDS}}}')
        query = "query{" + " ".join(parts) + "}"
        try:
            data = http_json(GITHUB_GRAPHQL, {"query": query}, {"Authorization": f"bearer {token}"}).get("data") or {}
        except urllib.error.HTTPError as e:
            log(f"  github error: {e}")
            data = {}
        for j, r in enumerate(chunk):
            out[r] = data.get(f"r{j}")
    return out


CC_RE = re.compile(r"claude[ -]?code|\.claude/|claude-agent-sdk|claude agent sdk|anthropic", re.I)
OTHER_RE = re.compile(r"\b(codex|cursor|copilot|gemini[ -]cli|opencode|windsurf|aider|cline|openclaw)\b", re.I)


def claude_code_focus(meta: dict) -> tuple[bool, str]:
    """True when the repo is mainly about Claude Code, not a generic or multi-agent tool."""
    name = meta["nameWithOwner"]
    topics = " ".join(n["topic"]["name"] for n in ((meta.get("repositoryTopics") or {}).get("nodes") or []))
    head = f"{name} {meta.get('description') or ''} {topics}"
    readme = ((meta.get("readme") or {}).get("text") or "")[:20000]
    cc = len(CC_RE.findall(readme)) + 5 * len(CC_RE.findall(head))
    other = len(OTHER_RE.findall(readme)) + 5 * len(OTHER_RE.findall(head))
    if cc == 0:
        return False, "does not mention Claude Code"
    if other > cc:
        return False, f"mostly about other agents ({other} vs {cc} mentions)"
    return True, f"{cc} Claude Code mentions"


def last_commit(meta: dict) -> str:
    ref = meta.get("defaultBranchRef") or {}
    return ((ref.get("target") or {}).get("committedDate")) or meta.get("pushedAt") or ""


def days_since(iso: str) -> int:
    if not iso:
        return 10**6
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - t).days


def fmt_stars(n: int) -> str:
    return f"{n / 1000:.1f}k+".replace(".0k", "k") if n >= 1000 else str(n)


# ---------------------------------------------------------------- commands


def cmd_discover(args) -> None:
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        sys.exit("TAVILY_API_KEY is not set.")
    token = github_token()
    known = readme_repos()
    known_urls = readme_urls()

    jobs = [(sec, q) for sec, qs in QUERIES.items() for q in qs]
    if args.limit:
        jobs = jobs[: args.limit]
    for month in months_since(args.since):
        for q in MONTHLY_QUERIES:
            jobs.append(("monthly", q.format(month=month)))
    log(f"Running {len(jobs)} Tavily searches...")

    found: dict[str, dict] = {}  # repo -> {sections, sources}
    articles: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda j: (j, tavily_search(j[1], key, args.since)), jobs))
    for (sec, q), hits in results:
        for h in hits:
            url = h.get("url", "")
            text = f"{url}\n{h.get('content', '')}"
            for r in repos_in(text):
                e = found.setdefault(r, {"sections": set(), "sources": set()})
                e["sections"].add(sec)
                e["sources"].add(url)
            if "github.com" not in url and url.rstrip("/").lower() not in known_urls:
                a = articles.setdefault(url, {"title": h.get("title", ""), "score": 0, "sections": set(), "date": h.get("published_date", "")})
                a["score"] = max(a["score"], h.get("score", 0))
                a["sections"].add(sec)

    if args.github_sweep:
        log("Sweeping GitHub search for new Claude Code repos (takes a few minutes)...")
        for r in github_search_sweep(args.since, token):
            found.setdefault(r, {"sections": {"github-search"}, "sources": {"github search"}})
    new_repos = sorted(r for r in found if r.lower() not in known)
    log(f"Found {len(found)} repos, {len(new_repos)} not yet in README. Checking GitHub...")
    meta = github_meta(new_repos, token)

    keep, rejected = [], []
    for r in new_repos:
        m = meta.get(r)
        if not m:
            rejected.append((r, "not found"))
            continue
        owner = m["nameWithOwner"].split("/")[0].lower()
        official = owner in OFFICIAL_OWNERS
        age = days_since(last_commit(m))
        if m["isArchived"]:
            rejected.append((r, "archived"))
        elif m["isFork"] and not official:
            rejected.append((r, "fork"))
        elif age > args.days and not official:
            rejected.append((r, f"no commit in {age} days"))
        elif m["stargazerCount"] < args.min_stars and not official:
            rejected.append((r, f"{m['stargazerCount']} stars"))
        elif not claude_code_focus(m)[0]:
            rejected.append((r, claude_code_focus(m)[1]))
        else:
            keep.append((m, sorted(found[r]["sections"]), official, age))
    keep.sort(key=lambda x: (not x[2], -x[0]["stargazerCount"]))

    lines = [
        f"# Research agent candidates ({dt.date.today().isoformat()})",
        "",
        f"Rules: official (anthropics/*, modelcontextprotocol/*) or {args.min_stars}+ stars, "
        f"not archived, not a fork, last commit within {args.days} days, mainly about Claude Code "
        f"(not generic or multi-agent), not already in README. Search window starts {args.since}.",
        "",
        f"- Searches: {len(jobs)}",
        f"- Repos found: {len(found)} ({len(new_repos)} new)",
        f"- Candidates: {len(keep)}",
        "",
        "## Candidate repos",
        "",
        "| Repo | Stars | Last commit | Sections | Description |",
        "|------|-------|-------------|----------|-------------|",
    ]
    for m, secs, official, age in keep:
        desc = (m.get("description") or "").replace("|", "\\|").strip()
        tag = " (official)" if official else ""
        lines.append(f"| [{m['nameWithOwner']}]({m['url']}){tag} | {fmt_stars(m['stargazerCount'])} | {age}d ago | {', '.join(secs)} | {desc} |")

    top_articles = sorted(articles.items(), key=lambda kv: -kv[1]["score"])[: args.articles]
    lines += ["", "## Articles and pages not yet in README (top by relevance)", ""]
    for url, a in top_articles:
        date = f" ({a['date'][:16]})" if a.get("date") else ""
        lines.append(f"- [{a['title'] or url}]({url}){date} — {', '.join(sorted(a['sections']))}")

    lines += ["", f"<details><summary>Rejected repos ({len(rejected)})</summary>", ""]
    lines += [f"- {r}: {why}" for r, why in rejected]
    lines += ["", "</details>", ""]
    write_out(args.out, "\n".join(lines))
    if args.json:
        write_out(args.json, json.dumps([{"repo": m["nameWithOwner"], "stars": m["stargazerCount"], "days_since_commit": age, "sections": secs, "official": off, "description": m.get("description")} for m, secs, off, age in keep], indent=2))


def cmd_stale(args) -> None:
    token = github_token()
    repos = sorted(readme_repos())
    log(f"Checking {len(repos)} repos in README...")
    meta = github_meta(repos, token)
    rows = []
    for r in repos:
        m = meta.get(r)
        official = r.split("/")[0] in OFFICIAL_OWNERS
        if not m:
            rows.append((r, "missing", "", 0))
            continue
        age = days_since(last_commit(m))
        if m["isArchived"]:
            rows.append((r, "archived", last_commit(m)[:10], m["stargazerCount"]))
        elif age > args.days and not official:
            rows.append((r, f"no commit in {age} days", last_commit(m)[:10], m["stargazerCount"]))
    lines = [f"# Stale repos in README ({dt.date.today().isoformat()})", "",
             f"Rule: archived, missing, or no commit in {args.days} days (official repos are kept unless archived).", "",
             f"{len(rows)} of {len(repos)} repos are stale.", ""]
    lines += [f"- {r} — {why} (last {last or 'n/a'}, {stars} stars)" for r, why, last, stars in rows]
    write_out(args.out, "\n".join(lines) + "\n")
    if args.json:
        write_out(args.json, json.dumps([r for r, *_ in rows], indent=2))


def write_out(path: str | None, text: str) -> None:
    if not path:
        print(text)
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    log(f"Wrote {path}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="find new resources with Tavily")
    d.add_argument("--days", type=int, default=90, help="max days since last commit (default 90)")
    d.add_argument("--min-stars", type=int, default=MIN_STARS)
    d.add_argument("--since", default=(dt.date.today() - dt.timedelta(days=90)).isoformat(),
                   help="search window start, YYYY-MM-DD (default 90 days ago)")
    d.add_argument("--articles", type=int, default=40, help="how many articles to list")
    d.add_argument("--limit", type=int, default=0, help="run only the first N queries (testing)")
    d.add_argument("--github-sweep", action="store_true",
                   help="also search GitHub for repos created since --since with 100+ stars")
    d.add_argument("--out")
    d.add_argument("--json")
    d.set_defaults(func=cmd_discover)
    s = sub.add_parser("stale", help="list stale repos already in README")
    s.add_argument("--days", type=int, default=90)
    s.add_argument("--out")
    s.add_argument("--json")
    s.set_defaults(func=cmd_stale)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
