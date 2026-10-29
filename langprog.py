#!/usr/bin/env python3
"""
langprog.py - your language garden 🌱

Scans your git repos, counts the lines *you* wrote in each language, and
writes a cute Markdown page (LANGUAGES.md) with two charts:

  1. Language journey - one dot per project, one row per language, over time
  2. Yearly mix       - what share of your code each language was, per year

Only your own commits count (matched by author email), so forks and
course templates don't drown your work in other people's code.

No dependencies beyond Python 3.9+ and git.

Usage
-----
  # scan repos you already have cloned in one folder
  python langprog.py --repos-dir ~/code --author "stephwyatt08@|45020090\\+" --name Steph

  # or let it list + clone a GitHub user's public repos (uses the GitHub API)
  python langprog.py --github-user stevwya77 --author "stephwyatt08@|45020090\\+" --name Steph

  # include private repos too (needs a token in LANGPROG_TOKEN, see README)
  python langprog.py --github-user stevwya77 --private --author "..." --name Steph

Output goes to ./langprog-out/ unless you pass --out.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Which file extensions count, and which "family" they belong to.
#    Families keep the chart to <= 8 colors. Anything not listed is ignored
#    (data files, lockfiles, markdown, images, configs...).
# ---------------------------------------------------------------------------

FAMILIES = [
    # (family name, emoji, extensions)
    ("Python",            "🐍", {"py", "ipynb", "pyw"}),
    ("JavaScript",        "🟨", {"js", "jsx", "mjs", "cjs"}),
    ("HTML & CSS",        "🎨", {"html", "htm", "css", "scss", "sass", "less", "astro", "vue", "svelte"}),
    ("TypeScript",        "🔷", {"ts", "tsx", "mts", "cts"}),
    ("C / C++",           "⚙️", {"c", "h", "cpp", "cc", "cxx", "hpp", "hh", "ino"}),
    ("Rust",              "🦀", {"rs"}),
    ("Hardware & ASM",    "🔌", {"hdl", "asm", "s", "v", "sv", "vhd", "vhdl", "tst", "vm", "jack"}),
    ("Other code",        "🧩", {"java", "cs", "go", "rb", "php", "swift", "kt", "lua", "sh",
                                 "bash", "zsh", "frag", "vert", "glsl", "sql", "r", "m", "jl"}),
]
EXT_TO_FAMILY = {ext: name for name, _, exts in FAMILIES for ext in exts}
FAMILY_EMOJI = {name: emo for name, emo, _ in FAMILIES}
FAMILY_ORDER = [name for name, _, _ in FAMILIES]

# Paths that are almost always generated or vendored, not hand-written.
SKIP_PATH = re.compile(
    r"(^|/)(node_modules|vendor|dist|build|out|\.next|\.astro|target|venv|\.venv|"
    r"__pycache__|site-packages|third_party|external|deps)/|\.min\.(js|css)$",
    re.I,
)

# Validated categorical palette (fixed order = fixed color per family).
PALETTE = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
    "dark":  ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
}
THEME = {
    "light": dict(bg="#fcfcfb", ink="#0b0b0b", ink2="#52514e", muted="#8a8984", grid="#e9e8e4", ring="#fcfcfb"),
    "dark":  dict(bg="#1a1a19", ink="#f0efec", ink2="#c3c2b7", muted="#8f8e88", grid="#2f2f2d", ring="#1a1a19"),
}
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"


# ---------------------------------------------------------------------------
# 2. Data model
# ---------------------------------------------------------------------------

@dataclass
class Project:
    name: str
    first: dt.date | None = None                       # your first commit
    last: dt.date | None = None                        # your latest commit
    commits: int = 0
    private: bool = False
    lines: dict = field(default_factory=lambda: defaultdict(int))        # family -> lines added
    started: dict = field(default_factory=dict)                          # family -> first date
    by_year: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))  # year -> family -> lines

    @property
    def total(self) -> int:
        return sum(self.lines.values())

    @property
    def main(self) -> str | None:
        return max(self.lines, key=self.lines.get) if self.lines else None


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, errors="replace", check=False).stdout


def notebook_code_lines(repo: Path, path: str) -> int:
    """.ipynb files are JSON; count only the code-cell lines in the latest version."""
    raw = git(repo, "show", f"HEAD:{path}")
    try:
        nb = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    n = 0
    for cell in nb.get("cells", []):
        if cell.get("cell_type") == "code":
            src = cell.get("source", "")
            src = "".join(src) if isinstance(src, list) else src
            n += sum(1 for line in src.splitlines() if line.strip())
    return n


def scan_repo(repo: Path, author_re: re.Pattern) -> Project:
    """Walk every commit you authored and tally added lines by language family."""
    proj = Project(repo.name)
    # One header line per commit ("@@<sha>|<date>|<email>"), then its numstat rows.
    log = git(repo, "log", "--all", "--no-merges", "--no-renames", "--numstat",
              "--format=@@%H|%as|%ae|%an")
    mine = False
    notebooks: dict[str, dt.date] = {}
    for line in log.splitlines():
        if line.startswith("@@"):
            _, date, email, name = line[2:].split("|", 3)
            mine = bool(author_re.search(email) or author_re.search(name))
            if mine:
                day = dt.date.fromisoformat(date)
                proj.commits += 1
                proj.first = min(proj.first or day, day)
                proj.last = max(proj.last or day, day)
            continue
        if not mine or not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3 or parts[0] == "-":           # "-" = binary file
            continue
        added, path = int(parts[0]), parts[2]
        if SKIP_PATH.search(path):
            continue
        ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        fam = EXT_TO_FAMILY.get(ext)
        if not fam:
            continue
        if ext == "ipynb":                                # handled separately below
            notebooks[path] = min(notebooks.get(path, day), day)
            proj.started[fam] = min(proj.started.get(fam, day), day)
            continue
        proj.lines[fam] += added
        proj.by_year[day.year][fam] += added
        proj.started[fam] = min(proj.started.get(fam, day), day)

    for path, day in notebooks.items():
        n = notebook_code_lines(repo, path)
        proj.lines["Python"] += n
        proj.by_year[day.year]["Python"] += n
    proj.lines = defaultdict(int, {k: v for k, v in proj.lines.items() if v > 0})
    proj.started = {k: v for k, v in proj.started.items() if k in proj.lines}
    return proj


def clone_github_user(user: str, dest: Path, extra: list[str], skip: list[str],
                      private: bool = False) -> tuple[list[Path], set[str]]:
    """List a user's repos via the GitHub API and clone/update them.

    Without --private: public repos only (any token, or none, works).
    With --private: uses LANGPROG_TOKEN to list and clone your private repos too.
    Returns (repo folders, names of the private ones).
    """
    dest.mkdir(parents=True, exist_ok=True)
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("LANGPROG_TOKEN") if private else os.environ.get("GITHUB_TOKEN")
    if private and not token:
        sys.exit("--private needs a GitHub token in the LANGPROG_TOKEN environment variable (see README).")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if private:   # /user/repos = everything the token can see: public + private, owned + collaborator
        base = "https://api.github.com/user/repos?visibility=all&affiliation=owner,collaborator"
    else:
        base = f"https://api.github.com/users/{user}/repos?type=owner"
    repos, page = {}, 1
    while True:
        with urllib.request.urlopen(urllib.request.Request(f"{base}&per_page=100&page={page}",
                                                           headers=headers)) as r:
            batch = json.load(r)
        if not batch:
            break
        repos.update({b["full_name"]: b.get("private", False) for b in batch})
        page += 1
    for full in extra:
        repos.setdefault(full, False)

    # Send the token as a header instead of putting it in the clone URL,
    # so it never gets saved inside the cloned repo's config.
    auth = []
    if private:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        auth = ["-c", f"http.extraHeader=Authorization: Basic {basic}"]

    paths, private_names = [], set()
    for full, is_private in repos.items():
        name = full.split("/")[1]
        if name in skip:
            continue
        p = dest / name
        if p.exists():
            subprocess.run(["git", *auth, "-C", str(p), "fetch", "--quiet", "--all"], check=False)
        else:
            subprocess.run(["git", *auth, "clone", "--quiet", "--no-checkout",
                            f"https://github.com/{full}.git", str(p)], check=False)
        if p.exists():
            paths.append(p)
            if is_private:
                private_names.add(name)
    return paths, private_names


# ---------------------------------------------------------------------------
# 3. Time axis that squishes long breaks (e.g. 2019 -> 2025) into a 💤 gap,
#    so the active periods get the room.
# ---------------------------------------------------------------------------

def month_index(d: dt.date) -> int:
    return d.year * 12 + d.month - 1


class GapAxis:
    """Piecewise-linear x axis: active stretches are to scale, gaps > `gap` months are squished."""

    def __init__(self, dates: list[dt.date], x0: float, x1: float, gap=12, gap_px=46):
        months = sorted({month_index(d) for d in dates})
        segs, start, prev = [], months[0], months[0]
        for m in months[1:]:
            if m - prev > gap:
                segs.append((start - 1, prev + 2))
                start = m
            prev = m
        segs.append((start - 1, prev + 2))
        self.segs = segs
        span = sum(b - a for a, b in segs)
        self.ppm = (x1 - x0 - gap_px * (len(segs) - 1)) / span       # pixels per month
        self.offsets, x = [], x0
        for a, b in segs:
            self.offsets.append(x)
            x += (b - a) * self.ppm + gap_px
        self.gap_px = gap_px

    def x(self, d: dt.date) -> float:
        m = month_index(d) + (d.day - 1) / 31
        for (a, b), off in zip(self.segs, self.offsets):
            if a <= m <= b:
                return off + (m - a) * self.ppm
        return self.offsets[-1]

    def segment_bounds(self, x: float) -> tuple[float, float]:
        """Left/right pixel edges of the active stretch containing x."""
        for (a, b), off in zip(self.segs, self.offsets):
            if off <= x <= off + (b - a) * self.ppm:
                return off, off + (b - a) * self.ppm
        return self.offsets[0], self.offsets[-1]

    def gaps(self):
        """(x_left, x_right, years_skipped) for each squished break."""
        out = []
        for i in range(len(self.segs) - 1):
            left = self.offsets[i] + (self.segs[i][1] - self.segs[i][0]) * self.ppm
            yrs = (self.segs[i + 1][0] - self.segs[i][1]) / 12
            out.append((left, self.offsets[i + 1], yrs))
        return out

    def ticks(self):
        """A tick at each January / July that falls inside an active stretch."""
        for a, b in self.segs:
            for m in range(a + 1, b):
                y, mo = divmod(m, 12)
                if mo in (0, 6):
                    yield dt.date(y, mo + 1, 1), (str(y) if mo == 0 else "Jul")


# ---------------------------------------------------------------------------
# 4. Charts (plain SVG strings, one per theme)
# ---------------------------------------------------------------------------

def svg_open(w, h, t, title, desc):
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-labelledby="t d" font-family="{FONT}">',
        f'<title id="t">{escape(title)}</title><desc id="d">{escape(desc)}</desc>',
        f'<rect width="{w}" height="{h}" rx="18" fill="{t["bg"]}"/>',
    ]


def fmt_lines(n: int) -> str:
    return f"{n/1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def journey_svg(projects: list[Project], mode: str, who: str) -> str:
    t, pal = THEME[mode], PALETTE[mode]
    fams = [f for f in FAMILY_ORDER if any(f in p.lines for p in projects)]
    W, left, right, top, row = 900, 150, 36, 78, 46
    H = top + row * len(fams) + 58
    dots = [(p, f, p.started[f], p.lines[f]) for p in projects for f in p.lines]
    axis = GapAxis([d for _, _, d, _ in dots], left + 14, W - right)
    max_lines = max(n for *_, n in dots)

    o = svg_open(W, H, t, f"{who}'s language journey",
                 "One dot per project per language, placed at the first commit in that language. "
                 "Dot size shows lines written.")
    o.append(f'<text x="28" y="40" font-size="20" font-weight="600" fill="{t["ink"]}">{escape(who)}\'s language journey</text>')
    o.append(f'<text x="28" y="61" font-size="13" fill="{t["ink2"]}">Each dot is a project, placed on first language commit. Bigger dot = more lines of code.</text>')

    bottom = top + row * len(fams)
    # gridlines + ticks
    for d, label in axis.ticks():
        x = axis.x(d)
        o.append(f'<line x1="{x:.1f}" y1="{top - 4}" x2="{x:.1f}" y2="{bottom}" stroke="{t["grid"]}" stroke-width="1"/>')
        weight = "600" if label != "Jul" else "400"
        color = t["ink2"] if label != "Jul" else t["muted"]
        o.append(f'<text x="{x:.1f}" y="{bottom + 20}" font-size="12" text-anchor="middle" font-weight="{weight}" fill="{color}">{label}</text>')
    # squished gaps
    for gl, gr, yrs in axis.gaps():
        cx = (gl + gr) / 2
        zig = " ".join(f"{cx + (4 if i % 2 else -4):.1f},{top - 4 + i * 8}" for i in range((bottom - top + 4) // 8 + 1))
        o.append(f'<polyline points="{zig}" fill="none" stroke="{t["muted"]}" stroke-width="1" stroke-linejoin="round"/>')
        o.append(f'<text x="{cx:.1f}" y="{bottom + 20}" font-size="12" text-anchor="middle" fill="{t["muted"]}">💤 {yrs:.0f} yrs</text>')

    for i, fam in enumerate(fams):
        cy = top + row * i + row / 2
        color = pal[FAMILY_ORDER.index(fam)]
        o.append(f'<line x1="{left}" y1="{cy}" x2="{W - right}" y2="{cy}" stroke="{t["grid"]}" stroke-width="1"/>')
        o.append(f'<circle cx="28" cy="{cy}" r="5" fill="{color}"/>')
        o.append(f'<text x="40" y="{cy + 4.5}" font-size="13" font-weight="500" fill="{t["ink"]}">{escape(fam)}</text>')
        lane = sorted([d for d in dots if d[1] == fam], key=lambda d: -d[3])  # big first, small on top
        for p, f, day, n in lane:
            r = 4 + 12 * math.sqrt(n / max_lines)
            x = axis.x(day)
            o.append(f'<circle cx="{x:.1f}" cy="{cy}" r="{r:.1f}" fill="{color}" fill-opacity="0.9" '
                     f'stroke="{t["ring"]}" stroke-width="2"><title>{escape(p.name)} · {escape(fam)} · '
                     f'{n:,} lines · started {day:%b %Y}</title></circle>')
        # label the biggest project in each lane (selective direct label)
        p, f, day, n = lane[0]
        r = 4 + 12 * math.sqrt(n / max_lines)
        x = axis.x(day)
        half = len(p.name) * 3.3                       # rough half-width of the label
        lo, hi = axis.segment_bounds(x)                # keep labels out of the 💤 gap
        lx = min(max(x, lo + half), hi - half)
        o.append(f'<text x="{lx:.1f}" y="{cy - r - 5:.1f}" font-size="11" text-anchor="middle" '
                 f'fill="{t["ink2"]}">{escape(p.name)}</text>')
    o.append("</svg>")
    return "\n".join(o)


def mix_svg(years: dict[int, dict[str, int]], mode: str) -> str:
    """100% stacked horizontal bar per year."""
    t, pal = THEME[mode], PALETTE[mode]
    fams = [f for f in FAMILY_ORDER if any(f in y for y in years.values())]
    W, left, right, top, row, bar = 900, 70, 90, 106, 40, 22
    H = top + row * len(years) + 24
    o = svg_open(W, H, t, "Language mix by year", "Share of lines written in each language, per year.")
    o.append(f'<text x="28" y="40" font-size="20" font-weight="600" fill="{t["ink"]}">🧺 Language mix by year</text>')
    # legend
    lx = 28
    for fam in fams:
        o.append(f'<rect x="{lx}" y="62" width="10" height="10" rx="2" fill="{pal[FAMILY_ORDER.index(fam)]}"/>')
        o.append(f'<text x="{lx + 15}" y="71.5" font-size="12" fill="{t["ink2"]}">{escape(fam)}</text>')
        lx += 34 + len(fam) * 7.2
    span = W - left - right
    for i, (yr, mix) in enumerate(sorted(years.items())):
        y = top + row * i
        total = sum(mix.values())
        o.append(f'<text x="28" y="{y + bar / 2 + 4.5}" font-size="13" font-weight="600" fill="{t["ink"]}">{yr}</text>')
        x = left
        segs = [(f, mix[f]) for f in fams if mix.get(f)]
        for j, (fam, n) in enumerate(segs):
            w = span * n / total
            gap = 2 if j < len(segs) - 1 else 0
            ww = max(w - gap, 1)
            rx = 4 if (j == 0 or j == len(segs) - 1) else 0
            o.append(f'<rect x="{x:.1f}" y="{y}" width="{ww:.1f}" height="{bar}" rx="{min(rx, ww/2):.1f}" '
                     f'fill="{pal[FAMILY_ORDER.index(fam)]}"><title>{yr} · {escape(fam)} · '
                     f'{n:,} lines ({100*n/total:.0f}%)</title></rect>')
            pct = 100 * n / total
            label = f"{pct:.0f}%"
            if ww > 44:
                txt = "#0b0b0b" if FAMILY_ORDER.index(fam) in (2, 3, 4) else "#ffffff"
                o.append(f'<text x="{x + ww/2:.1f}" y="{y + bar/2 + 4}" font-size="11" font-weight="600" '
                         f'text-anchor="middle" fill="{txt}">{label}</text>')
            x += w
        o.append(f'<text x="{W - right + 10}" y="{y + bar/2 + 4.5}" font-size="12" fill="{t["ink2"]}">{fmt_lines(total)} lines</text>')
    o.append("</svg>")
    return "\n".join(o)


# ---------------------------------------------------------------------------
# 5. The Markdown page
# ---------------------------------------------------------------------------

def picture(name: str, alt: str) -> str:
    return (f'<picture>\n'
            f'  <source media="(prefers-color-scheme: dark)" srcset="assets/{name}-dark.svg">\n'
            f'  <img alt="{escape(alt)}" src="assets/{name}-light.svg" width="100%">\n'
            f'</picture>')


def build_markdown(projects: list[Project], who: str) -> str:
    total = sum(p.total for p in projects)
    fams_seen = sorted({f for p in projects for f in p.lines}, key=FAMILY_ORDER.index)

    # When was each language "unlocked"?
    unlocks = []
    for fam in fams_seen:
        users = [p for p in projects if fam in p.lines]
        first = min(users, key=lambda p: p.started[fam])
        unlocks.append((first.started[fam], fam, first.name, len(users), sum(p.lines[fam] for p in users)))
    unlocks.sort()

    # Who's the current favorite? (most lines in the last 12 months)
    latest = max(p.last for p in projects)
    recent = defaultdict(int)
    for p in projects:
        for f, n in p.lines.items():
            if (latest - p.started[f]).days <= 365:
                recent[f] += n
    fave = max(recent, key=recent.get) if recent else None
    first_year = min(p.first for p in projects).year

    md = [f"# 🌱 {who}'s Language Garden", ""]
    md.append(f"> **{len(projects)} projects** · **{len(fams_seen)} language families** · "
              f"**{total:,} lines** written by me since {first_year}"
              + (f" · current favorite: {FAMILY_EMOJI[fave]} **{fave}**" if fave else ""))
    md += ["", picture("journey", f"{who}'s language journey: one dot per project, by language over time"), ""]
    md += ["## 🔓 Languages unlocked", "",
           "| | Language | First project | Unlocked | Projects | Lines |",
           "|---|---|---|---|---:|---:|"]
    for day, fam, name, nproj, n in unlocks:
        md.append(f"| {FAMILY_EMOJI[fam]} | **{fam}** | `{name}` | {day:%b %Y} | {nproj} | {n:,} |")

    years: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in projects:
        for yr, mix in p.by_year.items():
            for f, n in mix.items():
                if f in p.lines:                       # respects --min-lines
                    years[yr][f] += n
    md += ["", "## 🧺 How the mix changed", "", picture("mix", "Language mix by year, as stacked bars"), ""]

    md += ["## 🗂️ Every project", "", "<details>", "<summary>Show all projects, newest first</summary>", "",
           "| Project | Started | Main language | Also used | My commits | Lines |",
           "|---|---|---|---|---:|---:|"]
    for p in sorted(projects, key=lambda p: p.first, reverse=True):
        others = ", ".join(f"{FAMILY_EMOJI[f]} {f}" for f in sorted(p.lines, key=p.lines.get, reverse=True)[1:])
        md.append(f"| `{p.name}` | {p.first:%b %Y} | {FAMILY_EMOJI[p.main]} {p.main} | {others or '—'} "
                  f"| {p.commits} | {p.total:,} |")
    md += ["", "</details>", "",
           f"<sub>🪴 Grown by <code>langprog.py</code> on {dt.date.today():%b %d, %Y}. "
           "Counts lines I added in my own commits (merges, data files, lockfiles and vendored code skipped). "
           "Notebooks count code cells only.</sub>", ""]
    return "\n".join(md), years


# ---------------------------------------------------------------------------
# 6. Main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Make a cute Markdown chart of your language progression.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--repos-dir", type=Path, help="folder full of git repos")
    src.add_argument("--github-user", help="GitHub username: list + clone their public repos")
    ap.add_argument("--extra", nargs="*", default=[], help="extra owner/repo to include (with --github-user)")
    ap.add_argument("--private", action="store_true",
                    help="with --github-user: include private repos (token in LANGPROG_TOKEN)")
    ap.add_argument("--private-repos", nargs="*", default=[],
                    help="with --repos-dir: names of repos to treat as private")
    ap.add_argument("--show-private-names", action="store_true",
                    help="show real names of private repos (default: '🔒 Private project N')")
    ap.add_argument("--author", required=True, help="regex matching your commit email or name")
    ap.add_argument("--name", default="My", help="your name for the page title")
    ap.add_argument("--exclude", nargs="*", default=[], help="repo names to skip")
    ap.add_argument("--min-lines", type=int, default=20, help="ignore a language in a project below this many lines")
    ap.add_argument("--out", type=Path, default=Path("langprog-out"))
    ap.add_argument("--cache", type=Path, default=Path(".langprog-cache"), help="where --github-user clones go")
    args = ap.parse_args(argv)

    if args.github_user:
        repos, private_names = clone_github_user(args.github_user, args.cache, args.extra,
                                                 args.exclude, args.private)
    else:
        repos = sorted(p for p in args.repos_dir.iterdir() if (p / ".git").exists())
        private_names = set(args.private_repos)
    author_re = re.compile(args.author, re.I)

    projects = []
    for repo in repos:
        if repo.name in args.exclude:
            continue
        p = scan_repo(repo, author_re)
        p.private = repo.name in private_names
        for f in [f for f, n in p.lines.items() if n < args.min_lines]:
            del p.lines[f]
            p.started.pop(f, None)
        status = f"{p.total:>6,} lines  {p.main or '-'}" if p.lines else "  (none of your code)"
        shown = "🔒 (private)" if p.private and not args.show_private_names else repo.name
        print(f"  {shown:<32}{status}", file=sys.stderr)   # Action logs are public too
        if p.lines:
            projects.append(p)
    if not projects:
        sys.exit("No commits matched --author. Check `git log --format='%ae %an'` for your identities.")

    # Private repos get a placeholder name (numbered oldest first) unless you opt in.
    if not args.show_private_names:
        hidden = sorted((p for p in projects if p.private), key=lambda p: p.first)
        for i, p in enumerate(hidden, 1):
            p.name = f"🔒 Private project {i}"
        if hidden:
            print(f"  ({len(hidden)} private project names hidden)", file=sys.stderr)

    md, years = build_markdown(projects, args.name)
    (args.out / "assets").mkdir(parents=True, exist_ok=True)
    for mode in ("light", "dark"):
        (args.out / "assets" / f"journey-{mode}.svg").write_text(journey_svg(projects, mode, args.name))
        (args.out / "assets" / f"mix-{mode}.svg").write_text(mix_svg(years, mode))
    (args.out / "LANGUAGES.md").write_text(md)
    (args.out / "data.json").write_text(json.dumps([
        {"project": p.name, "first": str(p.first), "last": str(p.last), "commits": p.commits,
         "lines": dict(p.lines), "by_year": {y: dict(m) for y, m in p.by_year.items()}, "started": {k: str(v) for k, v in p.started.items()}}
        for p in projects], indent=2))
    print(f"\n🌱 Wrote {args.out / 'LANGUAGES.md'} ({len(projects)} projects)", file=sys.stderr)


if __name__ == "__main__":
    main()
