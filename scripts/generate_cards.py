#!/usr/bin/env python3
"""Generate light and dark GitHub profile stats SVGs for Async23."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

USERNAME = os.environ.get("PROFILE_USER", "Async23")
OUT_DIR = Path(os.environ.get("OUT_DIR", "dist"))

GRAPHQL_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
      }
      restrictedContributionsCount
    }
    repositories(
      first: 60
      ownerAffiliations: OWNER
      privacy: PUBLIC
      orderBy: { field: PUSHED_AT, direction: DESC }
    ) {
      nodes {
        name
        isFork
        isArchived
        createdAt
        pushedAt
        languages(first: 10, orderBy: { field: SIZE, direction: DESC }) {
          edges {
            size
            node {
              name
              color
            }
          }
        }
        releases(first: 5, orderBy: { field: CREATED_AT, direction: DESC }) {
          nodes {
            tagName
            publishedAt
            createdAt
            isDraft
            author {
              login
            }
          }
        }
      }
    }
  }
}
"""

THEMES = {
    "light": {
        "surface": "#f6f8fa",
        "border": "#d1d9e0",
        "fg": "#1f2328",
        "muted": "#59636e",
        "link": "#0969da",
        "track": "#e5e9ee",
        "other": "#8c959f",
    },
    "dark": {
        "surface": "#161b22",
        "border": "#30363d",
        "fg": "#e6edf3",
        "muted": "#8b949e",
        "link": "#4493f8",
        "track": "#21262d",
        "other": "#6e7681",
    },
}

FALLBACK_LANG_COLORS = {
    "Python": "#3572A5",
    "TypeScript": "#3178c6",
    "JavaScript": "#f1e05a",
    "Go": "#00ADD8",
    "Swift": "#F05138",
    "Shell": "#89e051",
    "HTML": "#e34c26",
    "CSS": "#663399",
    "Rust": "#dea584",
}


def get_token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token.strip()
    return subprocess.check_output(["gh", "auth", "token"], text=True).strip()


def fetch_profile_data(login: str, token: str) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    from_dt = now - dt.timedelta(days=30)
    payload = json.dumps(
        {
            "query": GRAPHQL_QUERY,
            "variables": {
                "login": login,
                "from": from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"{login}-profile-generator",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if "errors" in body:
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    return body["data"]["user"]


def compute_languages(repos: list[dict], now: dt.datetime) -> tuple[list[dict], str]:
    cutoff = now - dt.timedelta(days=90)
    active_orig = []
    all_orig = []
    for repo in repos:
        if repo["isFork"] or repo["isArchived"] or repo["name"].lower() == USERNAME.lower():
            continue
        all_orig.append(repo)
        pushed = dt.datetime.fromisoformat(repo["pushedAt"].replace("Z", "+00:00"))
        if pushed >= cutoff:
            active_orig.append(repo)

    if len(active_orig) >= 3:
        selected, window_label = active_orig, "近 90 天"
    else:
        selected, window_label = all_orig, "公开原创仓库"

    totals: dict[str, int] = {}
    colors: dict[str, str] = {}
    for repo in selected:
        for edge in repo["languages"]["edges"]:
            lang = edge["node"]["name"]
            size = int(edge["size"])
            if size <= 0:
                continue
            totals[lang] = totals.get(lang, 0) + size
            if edge["node"].get("color"):
                colors[lang] = edge["node"]["color"]

    grand_total = sum(totals.values())
    if grand_total == 0:
        return [], window_label

    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    top = ranked[:5]
    rest_bytes = sum(v for _, v in ranked[5:])

    items = []
    for name, size in top:
        pct = size * 100.0 / grand_total
        items.append(
            {
                "name": name,
                "pct": pct,
                "color": colors.get(name) or FALLBACK_LANG_COLORS.get(name, "#8b949e"),
            }
        )
    if rest_bytes > 0 and (rest_bytes * 100.0 / grand_total) >= 0.8:
        items.append({"name": "Other", "pct": rest_bytes * 100.0 / grand_total, "color": None})
    return items, window_label


def compute_recent_releases(repos: list[dict], login: str) -> list[dict]:
    releases = []
    for repo in repos:
        if repo["isArchived"]:
            continue
        repo_created = repo["createdAt"]
        chosen = None
        for rel in repo["releases"]["nodes"]:
            if rel.get("isDraft"):
                continue
            author_login = (rel.get("author") or {}).get("login")
            published = rel.get("publishedAt") or rel.get("createdAt")
            if not published:
                continue
            # On forks, only include releases published by this user after fork creation
            if repo["isFork"]:
                if author_login and author_login.lower() != login.lower():
                    continue
                if published < repo_created:
                    continue
            chosen = {
                "repo": repo["name"],
                "tag": rel["tagName"],
                "published": published,
                "date_short": published[5:10],
            }
            break
        if chosen:
            releases.append(chosen)

    releases.sort(key=lambda r: r["published"], reverse=True)
    return releases[:4]


def truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def render_svg(
    theme_name: str,
    langs: list[dict],
    lang_window: str,
    releases: list[dict],
    recent_contribs: int,
) -> str:
    t = THEMES[theme_name]
    w, h = 880, 176
    card_w, card_h = 431, 175
    right_x = 448.5

    # Build segmented progress bar (x=20..411, inner width=391)
    bar_x, bar_y, bar_w, bar_h = 20, 48, 391, 9
    gap = 2
    n_seg = len(langs)
    usable_w = max(0, bar_w - gap * max(0, n_seg - 1))

    seg_widths = []
    if n_seg > 0:
        pct_sum = sum(item["pct"] for item in langs) or 100.0
        raw_widths = [usable_w * (item["pct"] / pct_sum) for item in langs]
        # Enforce a tiny minimum width so real non-zero segments remain visible
        min_w = 5.0
        adjusted = [max(min_w, rw) for rw in raw_widths]
        scale = usable_w / sum(adjusted)
        seg_widths = [aw * scale for aw in adjusted]

    bar_rects = []
    cursor = float(bar_x)
    for idx, (item, sw) in enumerate(zip(langs, seg_widths)):
        color = item["color"] or t["other"]
        # Snap last segment cleanly to bar_x + bar_w
        if idx == n_seg - 1:
            sw = max(1.0, (bar_x + bar_w) - cursor)
        bar_rects.append(
            f'<rect x="{cursor:.2f}" y="{bar_y}" width="{sw:.2f}" height="{bar_h}" fill="{color}" />'
        )
        cursor += sw + gap

    # Language legend: crisp 2-column x 3-row layout aligned to x=20..411
    legend_els = []
    col_x = [20, 225]
    col_end = [206, 411]
    row_y = [88, 118, 148]
    for idx, item in enumerate(langs[:6]):
        col = idx % 2
        row = idx // 2
        x = col_x[col]
        y = row_y[row]
        color = item["color"] or t["other"]
        pct_str = f"{item['pct']:.1f}%" if item["pct"] < 10 else f"{int(item['pct'] + 0.5)}%"
        name_txt = escape(truncate(item["name"], 15))
        legend_els.append(
            f'<g class="fade" style="animation-delay:{idx * 45}ms">'
            f'<circle cx="{x + 4.5}" cy="{y - 4}" r="4.2" fill="{color}" />'
            f'<text x="{x + 15}" y="{y}" class="item-name">{name_txt}</text>'
            f'<text x="{col_end[col]}" y="{y}" text-anchor="end" class="item-val">{pct_str}</text>'
            f"</g>"
        )

    # Recent releases rows: 4 evenly spaced rows ending on y=148
    rel_els = []
    rel_y_start = 64
    rel_step = 28
    for idx, rel in enumerate(releases[:4]):
        y = rel_y_start + idx * rel_step
        repo_txt = escape(truncate(rel["repo"], 22))
        tag_txt = escape(truncate(rel["tag"], 18))
        date_txt = escape(rel["date_short"])
        rel_els.append(
            f'<g class="fade" style="animation-delay:{(idx + 1) * 55}ms">'
            f'<text x="468" y="{y}" class="mono-repo">{repo_txt}</text>'
            f'<text x="859" y="{y}" text-anchor="end" class="mono-meta">'
            f'<tspan class="tag">{tag_txt}</tspan>'
            f'<tspan class="sep"> · </tspan>'
            f'<tspan class="date">{date_txt}</tspan>'
            f"</text>"
            f"</g>"
        )
    if not rel_els:
        rel_els.append(
            f'<text x="468" y="96" class="label">暂无公开 Release 记录</text>'
        )

    contrib_label = f"近 30 天 {recent_contribs} 次贡献"

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{escape(USERNAME)} active languages and recent releases">
  <defs>
    <clipPath id="lang-bar-clip">
      <rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="4.5" ry="4.5" />
    </clipPath>
  </defs>
  <style>
    .card {{ fill: {t["surface"]}; stroke: {t["border"]}; stroke-width: 1; }}
    .label {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "PingFang SC", "Noto Sans SC", monospace;
      font-size: 11.5px;
      fill: {t["muted"]};
      letter-spacing: 0.04em;
    }}
    .item-name {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Noto Sans SC", sans-serif;
      font-size: 12.5px;
      font-weight: 500;
      fill: {t["fg"]};
    }}
    .item-val {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      fill: {t["muted"]};
    }}
    .mono-repo {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12.5px;
      font-weight: 500;
      fill: {t["fg"]};
    }}
    .mono-meta {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }}
    .tag {{ fill: {t["link"]}; }}
    .sep, .date {{ fill: {t["muted"]}; }}
    .fade {{ opacity: 0; animation: fadeIn 0.35s ease forwards; }}
    @keyframes fadeIn {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
    @media (prefers-reduced-motion: reduce) {{ .fade {{ opacity: 1; animation: none; }} }}
  </style>

  <!-- Left Card: Active Languages -->
  <rect class="card" x="0.5" y="0.5" width="{card_w}" height="{card_h}" rx="6" />
  <text x="20" y="30" class="label">近期活跃技术栈</text>
  <text x="411" y="30" text-anchor="end" class="label">{escape(lang_window)}</text>
  <rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="4.5" fill="{t["track"]}" />
  <g clip-path="url(#lang-bar-clip)">
    {"".join(bar_rects)}
  </g>
  {"".join(legend_els)}

  <!-- Right Card: Recent Releases -->
  <rect class="card" x="{right_x}" y="0.5" width="{card_w}" height="{card_h}" rx="6" />
  <text x="468" y="30" class="label">近期发布节奏</text>
  <text x="859" y="30" text-anchor="end" class="label">{escape(contrib_label)}</text>
  {"".join(rel_els)}
</svg>
'''


def main() -> None:
    token = get_token()
    now = dt.datetime.now(dt.timezone.utc)
    user = fetch_profile_data(USERNAME, token)
    repos = user["repositories"]["nodes"]
    langs, lang_window = compute_languages(repos, now)
    releases = compute_recent_releases(repos, USERNAME)
    recent_contribs = int(
        user["contributionsCollection"]["contributionCalendar"]["totalContributions"]
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for theme_name in ("light", "dark"):
        svg = render_svg(theme_name, langs, lang_window, releases, recent_contribs)
        out_path = OUT_DIR / f"stats-{theme_name}.svg"
        out_path.write_text(svg, encoding="utf-8")
        print(f"Generated {out_path} ({len(svg)} bytes)")

    print("Languages:", [(i["name"], round(i["pct"], 1)) for i in langs])
    print("Releases:", [(r["repo"], r["tag"], r["date_short"]) for r in releases])
    print("Last-30d contributions:", recent_contribs)


if __name__ == "__main__":
    main()
