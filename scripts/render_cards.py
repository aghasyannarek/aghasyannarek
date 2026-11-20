#!/usr/bin/env python3
"""Render self-contained profile cards; --refresh updates their GitHub JSON input."""
from __future__ import annotations

import argparse
import base64
import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from html import escape
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SVG_NS = "http://www.w3.org/2000/svg"
WIDTH = 960
QUERY = """query($login:String!) {
  user(login:$login) {
    pinnedItems(first:6, types:REPOSITORY) { nodes { ... on Repository {
      name nameWithOwner description url homepageUrl isPrivate stargazerCount
      primaryLanguage { name }
    } } }
  }
}"""


class CalendarParser(HTMLParser):
    """Read exact counts from GitHub's public calendar, including anonymous activity."""
    def __init__(self):
        super().__init__()
        self.dates, self.counts = {}, {}
        self.target, self.label = None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "td" and "data-date" in attrs and "id" in attrs:
            self.dates[attrs["id"]] = attrs["data-date"]
        if tag == "tool-tip" and attrs.get("for"):
            self.target, self.label = attrs["for"], []

    def handle_data(self, value):
        if self.target:
            self.label.append(value)

    def handle_endtag(self, tag):
        if tag == "tool-tip" and self.target:
            label = "".join(self.label).strip()
            match = re.match(r"(No|[\d,]+) contributions? on\b", label)
            if match:
                self.counts[self.target] = 0 if match[1] == "No" else int(match[1].replace(",", ""))
            self.target = None

    def days(self):
        return [{"date": day, "count": self.counts[key]}
                for key, day in self.dates.items() if key in self.counts]


def public_calendar(username: str, first: date, last: date) -> list[dict]:
    result = {}
    for year in range(first.year, last.year + 1):
        start, end = max(first, date(year, 1, 1)), min(last, date(year, 12, 31))
        request = urllib.request.Request(
            f"https://github.com/users/{username}/contributions?from={start}&to={end}",
            headers={"User-Agent": "github-profile-cards", "Accept-Language": "en-US", "Accept": "text/html"},
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            parser = CalendarParser()
            parser.feed(response.read().decode("utf-8"))
        for day in parser.days():
            if start.isoformat() <= day["date"] <= end.isoformat():
                result[day["date"]] = day
    return [result[key] for key in sorted(result)]


def featured_repos(profile: dict, data: dict) -> list[dict]:
    excluded = {name.lower() for name in profile.get("excluded_repos", [])}
    return [repo for repo in data["featured_repos"]
            if repo["name"].lower() not in excluded and repo["full_name"].lower() not in excluded]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never leave a half-written snapshot or SVG on a failed render.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     delete=False) as temp:
        temp.write(value)
        temporary = Path(temp.name)
    temporary.chmod(0o644)
    temporary.replace(path)


def dump_json(path: Path, data: dict) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def refresh(path: Path, profile: dict) -> None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("--refresh needs GH_TOKEN or GITHUB_TOKEN (read access to public GitHub data).")
    zone = ZoneInfo(profile["timezone"])
    today = datetime.now(zone).date()
    first = date.fromisoformat(profile["contributions_since"])
    body = {"query": QUERY, "variables": {"login": profile["username"]}}
    request = urllib.request.Request(
        "https://api.github.com/graphql", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "github-profile-cards", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise ValueError(f"GitHub returned HTTP {error.code}; the saved JSON was not changed.") from None
    if payload.get("errors") or not payload.get("data", {}).get("user"):
        raise ValueError("GitHub returned an incomplete GraphQL response; the saved JSON was not changed.")
    user = payload["data"]["user"]
    days = public_calendar(profile["username"], first, today)
    repos = [
        {"name": repo["name"], "full_name": repo["nameWithOwner"], "url": repo["url"],
         "description": repo["description"] or "", "homepage": repo["homepageUrl"] or "",
         "language": (repo["primaryLanguage"] or {}).get("name", "Code"),
         "stars": repo["stargazerCount"]}
        for repo in user["pinnedItems"]["nodes"] if repo and not repo["isPrivate"]
    ]
    snapshot = {"version": 1, "username": profile["username"], "as_of": today.isoformat(),
                "timezone": profile["timezone"], "since": first.isoformat(),
                "source": f'https://github.com/users/{profile["username"]}/contributions',
                "days": days, "featured_repos": repos}
    snapshot["featured_repos"] = featured_repos(profile, snapshot)
    validate_data(snapshot, profile["username"])
    dump_json(path, snapshot)
    print(f"Refreshed {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}: {len(days)} days, {len(snapshot["featured_repos"])} public pinned projects")


def validate_data(data: dict, username: str) -> None:
    if data.get("version") != 1 or data.get("username") != username:
        raise ValueError("Contribution JSON version or username does not match profile.json.")
    last = date.fromisoformat(data["as_of"])
    first = date.fromisoformat(data.get("since", (last - timedelta(days=364)).isoformat()))
    if first > last:
        raise ValueError("Contribution history start must not be after as_of.")
    expected = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    days = data["days"]
    if [day["date"] for day in days] != expected:
        raise ValueError("Contribution JSON must contain every unique, consecutive date from since to as_of.")
    if any(type(day["count"]) is not int or day["count"] < 0 for day in days):
        raise ValueError("Contribution counts must be nonnegative integers.")
    names = set()
    for repo in data["featured_repos"]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", repo["name"]) or repo["name"].lower() in names:
            raise ValueError("Featured repository names must be unique GitHub repository names.")
        names.add(repo["name"].lower())
        if repo["url"] != f'https://github.com/{repo["full_name"]}':
            raise ValueError("Featured repository URLs must point to their GitHub repository.")


def statistics(days: list[dict]) -> dict:
    monthly = defaultdict(int)
    for day in days:
        monthly[day["date"][:7]] += day["count"]
    # Today's zero does not end yesterday's streak while today is still in progress.
    index = len(days) - 1
    if days[index]["count"] == 0:
        index -= 1
    current = 0
    while index >= 0 and days[index]["count"] > 0:
        current += 1
        index -= 1
    longest = run = 0
    for day in days:
        run = run + 1 if day["count"] else 0
        longest = max(longest, run)
    busiest = max(days, key=lambda day: day["count"])  # Earliest day wins ties.
    return {"total": sum(day["count"] for day in days), "active": sum(day["count"] > 0 for day in days),
            "streak": current, "longest": longest,
            "busiest": busiest if busiest["count"] else None, "monthly": dict(monthly)}


def counted(value: int, noun: str) -> str:
    return f'{value:,} {noun}{"" if value == 1 else "s"}'


def text(x: float, y: float, value: object, size: int = 16, cls: str = "", **attrs) -> str:
    attributes = " ".join(f'{key.replace("_", "-")}="{escape(str(val), quote=True)}"' for key, val in attrs.items())
    return f'<text x="{x:g}" y="{y:g}" font-size="{size}" class="{cls}" {attributes}>{escape(str(value))}</text>'


def rect(x, y, width, height, **attrs) -> str:
    attributes = " ".join(f'{key.replace("_", "-")}="{escape(str(val), quote=True)}"' for key, val in attrs.items())
    return f'<rect x="{x:g}" y="{y:g}" width="{width:g}" height="{height:g}" {attributes}/>'


def arrow(x: int, y: int, size: int = 20) -> str:
    return f'<path d="M{x} {y+size}l{size} -{size}m-{size} 0h{size}v{size}" fill="none" stroke="currentColor" stroke-width="1.7"/>'


def stamp(number: str, label: str, width: int = WIDTH) -> str:
    return (text(36, 34, label, 11, "mono muted", letter_spacing="1.5") +
            text(width - 36, 34, number, 11, "mono accent", text_anchor="end"))


def reveal(content: str, delay: float, extra: str = "") -> str:
    return f'<g class="reveal {extra}" style="animation-delay:{delay:.2f}s">{content}</g>'


@lru_cache(maxsize=64)
def subset_font(filename: str, characters: str) -> str:
    """Use the pyftsubset CLI entry module, with one exact glyph subset per card."""
    with tempfile.TemporaryDirectory() as temp:
        glyphs, output = Path(temp) / "glyphs.txt", Path(temp) / "subset.woff"
        glyphs.write_text(characters, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "fontTools.subset", str(ROOT / "assets/fonts" / filename),
             f"--text-file={glyphs}", f"--output-file={output}", "--flavor=woff",
             "--no-hinting", "--no-recalc-timestamp"], capture_output=True, text=True,
        )
        if result.returncode:
            raise ValueError(f"pyftsubset failed for {filename}: {result.stderr.strip()}")
        return base64.b64encode(output.read_bytes()).decode("ascii")


@dataclass
class Card:
    filename: str
    title: str
    description: str
    content: str
    height: int
    width: int = WIDTH
    href: str | None = None


def svg(card: Card, theme: dict, static: bool) -> str:
    source = ET.fromstring(f'<svg xmlns="{SVG_NS}">{card.content}</svg>')
    if static:
        for parent in source.iter():
            for child in list(parent):
                if child.tag in {f"{{{SVG_NS}}}animate", f"{{{SVG_NS}}}animateTransform"}:
                    parent.remove(child)
        # Base SVG attributes always describe the final frame.
        content = "".join(ET.tostring(child, encoding="unicode") for child in source)
    else:
        content = card.content
    characters = "".join(sorted(set(card.title + card.description + "".join(source.itertext()))))
    sans = subset_font("SpaceGrotesk.ttf", characters)
    mono = subset_font("IBMPlexMono-Regular.ttf", characters)
    styles = f"""
@font-face{{font-family:Space;src:url(data:font/woff;base64,{sans}) format('woff');font-weight:300 700;font-style:normal}}
@font-face{{font-family:Plex;src:url(data:font/woff;base64,{mono}) format('woff');font-weight:400;font-style:normal}}
:root{{color:{theme['text']}}}
text{{font-family:Space,sans-serif;fill:{theme['text']};font-weight:400}}
.mono{{font-family:Plex,monospace;font-weight:400}}
.bold{{font-weight:700;letter-spacing:-1.8px}}
.muted{{fill:{theme['muted']}}}
.accent{{fill:{theme['accent']};color:{theme['accent']}}}
.ink{{fill:{theme['background']}}}
.surface{{fill:{theme['surface']}}}
.line{{stroke:{theme['border']};fill:none}}
@keyframes arrive{{from{{opacity:0;transform:translateY(9px)}}to{{opacity:1;transform:translateY(0)}}}}
@keyframes grow{{from{{transform:scaleY(0)}}to{{transform:scaleY(1)}}}}
@keyframes cell{{from{{opacity:.12}}to{{opacity:1}}}}
.reveal{{animation:arrive .6s cubic-bezier(.2,.7,.2,1) both}}
.bar{{transform-box:fill-box;transform-origin:center bottom;animation:grow .7s cubic-bezier(.2,.7,.2,1) both}}
.day{{animation:cell .55s ease both}}
.smil-trace{{stroke-dashoffset:0}}
@media(prefers-reduced-motion:reduce){{
 *{{animation:none!important;transition:none!important}}
 .reveal,.day{{opacity:1!important;transform:none!important}}
 .bar{{transform:none!important}}
 /* CSS overrides the SMIL presentation value, fixing the trace at its final frame. */
 .smil-trace{{stroke-dashoffset:0!important}}
}}
"""
    if static:
        styles += "\n*{animation:none!important;transition:none!important}\n"
    return (f'<svg xmlns="{SVG_NS}" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{card.width}" height="{card.height}" viewBox="0 0 {card.width} {card.height}" '
            f'role="img" aria-labelledby="title desc">\n<title id="title">{escape(card.title)}</title>\n'
            f'<desc id="desc">{escape(card.description)}</desc>\n<style>{styles}</style>\n'
            + rect(.5, .5, card.width - 1, card.height - 1, rx=22,
                   fill=theme["background"], stroke=theme["border"])
            + content + "\n</svg>\n")


def hero(profile: dict, theme: dict) -> Card:
    accent = theme["accent"]
    name = profile["name"].split(" ", 1)
    body = stamp("00 / HELLO", "FRONTEND DEVELOPER · ARMENIA")
    body += reveal(text(36, 112, name[0], 76, "bold") +
                   text(32, 191, name[1] + "." if len(name) > 1 else "", 76, "bold"), .06)
    body += (f'<path class="smil-trace" d="M38 209H460" pathLength="1" stroke="{accent}" '
             'stroke-width="2" stroke-dasharray="1" stroke-dashoffset="0">'
             '<animate attributeName="stroke-dashoffset" from="1" to="0" begin=".3s" dur=".85s" fill="freeze"/>'
             '</path>')
    body += reveal(text(36, 248, profile["headline"], 17), .3)
    body += reveal(text(36, 290, " / ".join(profile["skills"]), 12, "mono muted") +
                   text(924, 290, "@" + profile["username"], 12, "mono muted", text_anchor="end"), .44)
    portrait = profile.get("portrait")
    if portrait:
        portrait_path = (ROOT / portrait).resolve()
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(portrait_path.suffix.lower())
        if not mime:
            raise ValueError("portrait must be a local PNG, JPEG, or WebP image.")
        uri = base64.b64encode(portrait_path.read_bytes()).decode("ascii")
        art = ('<defs><clipPath id="portrait"><rect x="716" y="64" width="164" height="154" rx="26"/></clipPath></defs>'
               f'<image x="716" y="64" width="164" height="154" preserveAspectRatio="xMidYMid slice" '
               f'clip-path="url(#portrait)" xlink:href="data:{mime};base64,{uri}"/>')
    else:
        art = '<g transform="translate(782 136)">'
        for radius in (57, 80, 103):
            art += f'<circle r="{radius}" class="line"/>'
        art += ('<path d="M-112 0H112M0 -106V106" class="line" stroke-dasharray="3 6"/>'
                f'<circle r="54" fill="{accent}"/>'
                '<path d="m-18 -13-14 13 14 13m36-26 14 13-14 13m-13 8 10-42" '
                f'fill="none" stroke="{theme["background"]}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
                f'<circle cx="67" cy="-44" r="5" fill="{accent}"/>'
                f'<circle cx="-89" cy="51" r="4" fill="{theme["text"]}"/></g>')
    body += reveal(art, .18)
    return Card("hero.svg", profile["name"], profile["headline"], body, 320)


def tiles(profile: dict) -> list[Card]:
    specs = [("github", "01 / EXPLORE", "The code", "Projects & experiments", f'https://github.com/{profile["username"]}?tab=repositories'),
             ("linkedin", "01 / CONNECT", "The conversation", "Find me on LinkedIn", profile["linkedin"]),
             ("email", "01 / SAY HELLO", "The next idea", "Let’s build something.", f'mailto:{profile["email"]}')]
    result = []
    for i, (name, label, title, detail, href) in enumerate(specs):
        body = reveal(text(22, 29, label, 10, "mono muted", letter_spacing=".7") +
                      text(22, 78, title, 24, "bold") + text(22, 108, detail, 12, "muted") +
                      f'<g class="accent">{arrow(266, 25, 13)}</g>', .55 + i * .12)
        result.append(Card(f"link-{name}.svg", title, detail, body, 136, 306, href))
    return result


def contributions(data: dict, stats: dict, theme: dict) -> Card:
    first, last = data["days"][0]["date"], data["as_of"]
    body = stamp("02 / ACTIVITY", "CONSISTENCY, IN CONTEXT")
    body += reveal(text(36, 81, f"Building since {first[:4]}.", 32, "bold") +
                   text(924, 77, f"{first} → {last}", 11, "mono muted", text_anchor="end"), .95)
    busiest = stats["busiest"]
    busiest_day = date.fromisoformat(busiest["date"]).strftime("%d %b %Y") if busiest else "No activity yet"
    values = [(f'{stats["total"]:,}', "CONTRIBUTIONS", f"Since {first[:4]}"),
              (counted(stats["streak"], "day"), "CURRENT STREAK", f'Best: {counted(stats["longest"], "day")}'),
              (str(stats["active"]), "ACTIVE DAYS", "Days with contributions"),
              (str(busiest["count"]) if busiest else "—", "BUSIEST DAY", busiest_day)]
    for i, (value, label, detail) in enumerate(values):
        x = 36 + i * 228
        body += reveal(text(x, 140, value, 32, "bold accent") +
                       text(x, 166, label, 10, "mono muted", letter_spacing=".8") +
                       text(x, 189, detail, 12, "muted"), 1.04 + i * .07)
    body += reveal(text(36, 237, "CONTRIBUTIONS / MONTH", 11, "mono muted", letter_spacing="1"), 1.25)
    monthly = stats["monthly"]
    years = sorted({month[:4] for month in monthly})
    max_count = max(monthly.values(), default=0)
    step = 830 / 12
    for row, year in enumerate(years):
        top = 260 + row * 142
        body += text(36, top + 52, year, 13, "mono")
        for y in (top + 12, top + 51, top + 90):
            body += f'<path d="M94 {y}H924" class="line" stroke-dasharray="2 6"/>'
        for month_number in range(1, 13):
            month = f"{year}-{month_number:02d}"
            x, width = 94 + (month_number - 1) * step + 7, step - 14
            count = monthly.get(month)
            if count is not None:
                height = 78 * count / max(1, max_count)
                body += rect(x, top + 12, width, 78, rx=5, fill=theme["surface"])
                if count:
                    body += (f'<g class="bar" style="animation-delay:{1.3+row*.15+month_number*.025:.3f}s"><title>{month}: {count} contributions</title>' +
                             rect(x, top + 90 - height, width, height, rx=min(5, height / 2), fill=theme["accent"]) + "</g>")
                body += reveal(text(x + width / 2, top + 4, str(count), 11, "mono muted", text_anchor="middle"), 1.4 + row*.15 + month_number*.025)
            body += text(x + width / 2, top + 114, calendar.month_abbr[month_number], 10, "mono muted", text_anchor="middle")
    description = (f'{counted(stats["total"], "contribution")}, current streak {counted(stats["streak"], "day")}, '
                   f'{counted(stats["active"], "active day")}. Busiest day: {busiest_day}' +
                   (f' with {counted(busiest["count"], "contribution")}.' if busiest else ".") +
                   f' Calendar dates {first} to {last}; boundary months are partial.')
    return Card("activity.svg", "Contributions per month", description, body, 248 + len(years) * 142)


def project_icon(kind: str, theme: dict) -> str:
    a, s, b = theme["accent"], theme["surface"], theme["border"]
    content = rect(0, 0, 132, 116, rx=16, fill=s)
    if kind == "chart":
        for x, height in ((25, 25), (49, 43), (73, 35), (97, 65)):
            content += rect(x - 7, 86 - height, 14, height, rx=3, fill=a, opacity=.35 + height / 100)
    elif kind == "dashboard":
        for x, y, w, h, opacity in ((18, 20, 24, 76, .25), (50, 20, 64, 20, .8), (50, 48, 28, 48, .4), (86, 48, 28, 48, .2)):
            content += rect(x, y, w, h, rx=4, fill=a, opacity=opacity)
    elif kind == "qr":
        for x, y in ((24, 16), (76, 16), (24, 68)):
            content += rect(x, y, 32, 32, rx=3, fill="none", stroke=a, stroke_width=3)
            content += rect(x + 10, y + 10, 12, 12, rx=1, fill=a)
        for x, y in ((78, 70), (90, 82), (102, 70), (78, 94), (102, 94)):
            content += rect(x, y, 10, 10, rx=1, fill=a, opacity=.65)
    elif kind == "quiz":
        for i in range(3):
            y = 22 + i * 28
            content += rect(20, y, 18, 18, rx=5, fill=a if i == 1 else "none", stroke=a if i == 1 else b)
            content += f'<path d="M49 {y+9}H108" stroke="{a}" stroke-opacity="{.85 if i == 1 else .25}" stroke-width="5" stroke-linecap="round"/>'
        content += f'<path d="m24 59 3 3 7-8" fill="none" stroke="{theme["background"]}" stroke-width="2"/>'
    elif kind == "weather":
        content += f'<circle cx="79" cy="40" r="17" fill="{a}"/>'
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            content += f'<path d="M{79+24*math.cos(rad):.2f} {40+24*math.sin(rad):.2f}l{6*math.cos(rad):.2f} {6*math.sin(rad):.2f}" stroke="{a}" stroke-width="2"/>'
        content += f'<path d="M32 87a16 16 0 0 1-1-32 24 24 0 0 1 46-3 18 18 0 1 1 10 35Z" fill="{s}" stroke="{a}" stroke-width="2"/>'
    else:
        content += f'<g transform="translate(45 38)" class="accent">{arrow(0, 0, 40)}</g>'
    return content


def projects(profile: dict, data: dict, theme: dict) -> list[Card]:
    result = []
    for i, repo in enumerate(featured_repos(profile, data)):
        custom = profile.get("projects", {}).get(repo["name"], {})
        description = custom.get("description") or repo["description"] or f'A project in {repo["language"]}.'
        # Long API descriptions are wrapped to actual font metrics, never clipped.
        description_lines = wrap_text(description, 675, 16)
        delay = 2.05 + i * .28
        body = (text(36, 34, f'FEATURED PROJECT {i+1:02d}', 11, "mono muted", letter_spacing="1.5") +
                text(886, 34, f'{i+3:02d} / SOURCE', 11, "mono accent", text_anchor="end"))
        title_size = min(30, int(660 / max(1, len(repo["name"])) * 1.65))
        body += text(36, 82, repo["name"], title_size, "bold")
        for line_index, line in enumerate(description_lines):
            body += text(36, 113 + line_index * 21, line, 16, "muted")
        tag_y = 151 + (len(description_lines) - 1) * 21
        body += text(36, tag_y, " / ".join(custom.get("tags", [repo["language"]])), 11, "mono accent")
        body += text(710, tag_y, f'{repo["stars"]} STARS', 10, "mono muted", text_anchor="end")
        body += f'<g transform="translate(772 48)">{project_icon(custom.get("icon", "code"), theme)}</g>'
        body += f'<g class="accent">{arrow(909, 24, 13)}</g>'
        result.append(Card(f'repo-{repo["name"].lower()}.svg', repo["full_name"], description,
                           reveal(body, delay), max(188, tag_y + 35), href=repo["url"]))
    return result


@lru_cache(maxsize=1)
def sans_metrics():
    from fontTools.ttLib import TTFont
    font = TTFont(ROOT / "assets/fonts/SpaceGrotesk.ttf")
    return font.getBestCmap(), font["hmtx"].metrics, font["head"].unitsPerEm


def wrap_text(value: str, max_width: float, size: int) -> list[str]:
    cmap, metrics, units = sans_metrics()
    def measure(string):
        return sum(metrics[cmap.get(ord(char), ".notdef")][0] for char in string) * size / units
    lines, line = [], ""
    for word in value.split():
        candidate = f"{line} {word}".strip()
        if measure(candidate) <= max_width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = ""
            # API descriptions may contain an unbroken URL or a very long word.
            for char in word:
                if line and measure(line + char) > max_width:
                    lines.append(line)
                    line = ""
                line += char
    if line:
        lines.append(line)
    return lines or [""]


def heat_color(count: int, peak: int, theme: dict) -> str:
    if not count:
        return theme["surface"]
    strength = .3 + .7 * math.sqrt(count / max(1, peak))
    start = tuple(int(theme["surface"][i:i+2], 16) for i in (1, 3, 5))
    end = tuple(int(theme.get("contribution", theme["accent"])[i:i+2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(a + (b-a)*strength):02x}" for a, b in zip(start, end))


def graph(data: dict, stats: dict, theme: dict, delay: float) -> Card:
    body = stamp("∞ / KEEP BUILDING", "THE CONTRIBUTION GRAPH")
    body += reveal(text(36, 79, "Small steps. Visible progress.", 29, "bold") +
                   text(924, 77, f'{stats["total"]} CONTRIBUTIONS', 11, "mono accent", text_anchor="end"), delay)
    by_year = defaultdict(list)
    for day in data["days"]:
        by_year[day["date"][:4]].append(day)
    peak = max(day["count"] for day in data["days"])
    for year_index, (year, days) in enumerate(sorted(by_year.items())):
        top = 104 + year_index * 164
        first = date(int(year), 1, 1)
        origin = first - timedelta(days=(first.weekday() + 1) % 7)
        # Each year gets a full-width calendar, preserving readable cells.
        body += text(36, top + 12, year, 13, "mono")
        previous_month = None
        for day in days:
            current = date.fromisoformat(day["date"])
            column, row = divmod((current - origin).days, 7)
            x, y = 70 + column * 16, top + 48 + row * 16
            month = current.strftime("%b")
            if month != previous_month:
                body += text(x, top + 35, month, 10, "mono muted")
            previous_month = month
            body += (f'<g class="day" style="animation-delay:{delay+.12+year_index*.15+column*.013:.3f}s">'
                     f'<title>{day["date"]}: {day["count"]} contributions</title>' +
                     rect(x, y, 12, 12, rx=2, fill=heat_color(day["count"], peak, theme)) + "</g>")
        for row, label in ((1, "M"), (3, "W"), (5, "F")):
            body += text(36, top + 57 + row * 16, label, 10, "mono muted")
    footer = 104 + len(by_year) * 164 + 22
    body += text(36, footer, f'AS OF {data["as_of"]} · {data["timezone"]}', 10, "mono muted")
    body += text(738, footer, "LESS", 10, "mono muted")
    for i, value in enumerate((0, 1, 4, 7, 10)):
        body += rect(783 + i * 18, footer - 11, 12, 12, rx=2, fill=heat_color(value, 10, theme))
    body += text(924, footer, "MORE", 10, "mono muted", text_anchor="end")
    return Card("contribution-graph.svg", "GitHub contribution graph",
                f'{stats["total"]} contributions from {data["days"][0]["date"]} to {data["as_of"]}. '
                'Green cells indicate contribution activity, including anonymous private activity shared on the public GitHub profile.',
                body, footer + 24)


def image_tag(card: Card, prefix: str, width: str = "100%") -> str:
    img = f'<img src="{escape(prefix + card.filename, quote=True)}" width="{width}" alt="{escape(card.title + ": " + card.description, quote=True)}">'
    return f'<a href="{escape(card.href, quote=True)}">{img}</a>' if card.href else img


def readme(cards: list[Card], prefix: str = "cards/") -> str:
    return ("<!-- Generated by scripts/render_cards.py. Edit profile.json, then render. -->\n\n" +
            "<p>\n" + image_tag(cards[0], prefix) + "\n</p>\n\n<p>\n" +
            "\n".join(image_tag(card, prefix, "32.5%") for card in cards[1:4]) +
            "\n</p>\n\n" + "\n\n".join("<p>\n" + image_tag(card, prefix) + "\n</p>" for card in cards[4:]) +
            '\n\n<details>\n<summary>About these cards</summary>\n\n'
            'Self-contained SVGs, refreshed daily from GitHub. [Build and preview instructions](docs/cards.md).\n\n</details>\n')


def preview_html(cards: list[Card], output: Path, preview: Path, theme: dict) -> None:
    prefix = Path(os.path.relpath(output, preview.parent)).as_posix() + "/"
    content = readme(cards, prefix)
    content = content[:content.index("<details>")]
    write_text(preview, f'<!doctype html><html lang="en"><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width, initial-scale=1">'
               '<title>Narek Aghasyan — profile preview</title>'
               f'<style>body{{margin:0;background:#0d1117;color:{theme["text"]};font-family:sans-serif}}'
               'main{max-width:960px;margin:32px auto;padding:24px;border:1px solid #30363d;border-radius:8px}'
               'img{max-width:100%;height:auto;vertical-align:middle}p{margin:16px 0}a{color:inherit}'
               '@media(max-width:600px){main{margin:0;padding:12px;border:0}}'
               '</style><main>' + content + '</main></html>\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data/contributions.json")
    parser.add_argument("--output", type=Path, default=ROOT / "cards")
    parser.add_argument("--preview", type=Path, help="Also write an HTML preview that embeds cards as images.")
    parser.add_argument("--refresh", action="store_true", help="Refresh JSON from GitHub, then exit; requires GH_TOKEN.")
    args = parser.parse_args()
    profile = read_json(ROOT / "profile.json")
    if args.refresh:
        refresh(args.data.resolve(), profile)
        return
    theme = profile["theme"]
    if any(not re.fullmatch(r"#[0-9a-fA-F]{6}", colour) for colour in theme.values()):
        raise ValueError("Theme colours must be six-digit hex values.")
    data = read_json(args.data)
    validate_data(data, profile["username"])
    stats = statistics(data["days"])
    cards = [hero(profile, theme), *tiles(profile), contributions(data, stats, theme),
             *projects(profile, data, theme),
             graph(data, stats, theme, 2.3 + len(featured_repos(profile, data)) * .28)]
    static = os.environ.get("STATIC") == "1"
    # Render everything before touching the existing cards.
    rendered = [(card, svg(card, theme, static)) for card in cards]
    output = args.output.resolve()
    manifest = output / "manifest.json"
    old_files = read_json(manifest).get("files", []) if manifest.exists() else []
    for card, markup in rendered:
        write_text(output / card.filename, markup)
    for obsolete in set(old_files) - {card.filename for card in cards}:
        if Path(obsolete).name == obsolete and obsolete.endswith(".svg"):
            (output / obsolete).unlink(missing_ok=True)
    dump_json(manifest, {"static": static, "as_of": data["as_of"], "files": [card.filename for card in cards]})
    if output == ROOT / "cards":
        write_text(ROOT / "README.md", readme(cards))
    if args.preview:
        preview_html(cards, output, args.preview.resolve(), theme)
    print(f'Rendered {len(cards)} {"static" if static else "animated"} cards into {output}')


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        sys.exit(f"Card build failed: {error}")
