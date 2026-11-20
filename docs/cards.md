# Profile cards

Edit `profile.json` for the name, one-line biography, skills, links, project copy,
and palette. Featured projects follow Narek’s public GitHub pins, except repositories listed
in `excluded_repos` (`chart` is excluded). The daily refresh preserves exclusions; optional copy and
illustration overrides are keyed by repository name in `profile.json`.

The palette follows GitHub dark: `#0D1117` backgrounds, `#161B22` surfaces,
`#30363D` borders, `#C9D1D9` text, blue `#58A6FF` accents, and green `#39D353` activity.
Space Grotesk and IBM Plex Mono are vendored with their SIL Open Font Licenses in
`assets/fonts/`. Every SVG embeds its own glyph subsets as base64 WOFF data URIs;
the generator invokes the `pyftsubset` entry module (`python -m fontTools.subset`).
Rendering needs no network, system fonts, GitHub token, or third-party card service.

## Build and screenshot

Use Python 3.12 or newer:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/render_cards.py
```

For a screenshot, build final frames into an ignored preview directory. This keeps
the animated files in `cards/` ready for GitHub:

```sh
STATIC=1 python scripts/render_cards.py --output .preview/cards --preview .preview/index.html
python -m http.server 8765 --bind 127.0.0.1
```

Open `http://127.0.0.1:8765/.preview/index.html` and screenshot it. The HTML uses
ordinary `<img>` elements, including three linked images at exactly `32.5%` width,
just like the README. It works at mobile widths as well.

`STATIC=1 python scripts/render_cards.py` also works directly on `cards/`. Run
`python scripts/render_cards.py` again before committing to restore motion. For
an animated browser preview, use
`python scripts/render_cards.py --preview .preview/animated.html`.

To add a portrait, set `portrait` in `profile.json` to a local PNG, JPEG, or WebP
path relative to the repository. The portrait is embedded as a data URI; `null`
uses the code illustration. Keep images small so the resulting SVG stays light.

## Data and daily updates

`data/contributions.json` contains every consecutive calendar date from
`contributions_since` (January 1, 2025) through `as_of`, plus public pinned
repository metadata. Each calendar year is fetched separately from GitHub’s public
contribution calendar. Exact daily counts come from the calendar’s accessible
tooltips, including anonymous private activity the user shares on their profile.
No private repository names or private repository metadata are requested or saved.
The refresh validates every expected date before replacing the JSON; an incomplete
calendar or changed page format fails without erasing the saved history. The action runs daily at 02:23 UTC (06:23 in Armenia), or through
**Actions → Refresh profile cards → Run workflow**. It refreshes JSON, runs the
same renderer, and commits the JSON, cards, and generated README. Pushes that
change the renderer, fonts, workflow, or profile also rebuild. Pull requests test
and render the saved snapshot without refreshing or committing.

The built-in `GITHUB_TOKEN` has the workflow’s `contents: write` permission for
committing; no personal access token is required. GitHub’s schedule needs the
workflow on the default branch, can be delayed, and may be disabled after 60 days
of repository inactivity. See [GitHub’s schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
If the repository has branch rules that prevent bot
pushes, allow this workflow under those rules. A failed API request or incomplete
calendar fails the job and leaves the saved JSON untouched.

Refresh locally, when desired:

```sh
GH_TOKEN="$(gh auth token)" python scripts/render_cards.py --refresh
python scripts/render_cards.py
```

Rendering reads JSON only. `--refresh` is a separate, network-only operation.
The calendar reflects GitHub’s public contribution visibility settings. It
includes anonymous private counts when shared publicly; no additional token scope
is needed for those counts. The built-in workflow token fetches public pinned
repository metadata through GraphQL. The end date uses `Asia/Yerevan`; daily
counts follow GitHub’s own calendar dates.

Monthly bars and contribution graphs have separate rows for each year since
2025, preserving readable labels and cells as history grows. Current months are
partial; future months have no bars. Active days have at least one contribution.
Current streak counts backwards from `as_of`, allowing today’s still-open date to
be empty if yesterday was active. Best streak and busiest day cover the entire
saved history. Busiest-day ties use the earliest date. Empty history displays
“No activity yet”. Green activity colours scale against the busiest day.

## Image compatibility and motion

Cards contain SVG geometry and text, one inline style block, CSS keyframes, and a
finite SMIL underline trace in the hero. They have no scripts, `foreignObject`,
external images, external styles, or external fonts. Links wrap the images in the
README because links inside an SVG image cannot serve as page links.

All base SVG attributes describe the final frame. CSS entrance, bar, and calendar
animations finish once, with increasing delays down the page. Under
`prefers-reduced-motion: reduce`, CSS animations and transitions are disabled and
a CSS `!important` rule pins the SMIL trace’s animated presentation value to its
final state. `STATIC=1` also removes SMIL elements. The result remains legible in
viewers that do not run animation. Image animations start when each image loads;
GitHub’s lazy loading, image cache, and browser throttling of offscreen images can
affect their relative start times.

SVG image constraints follow [MDN’s SVG image reference](https://developer.mozilla.org/en-US/docs/Web/SVG/Guides/SVG_as_an_image).

Run the checks with:

```sh
python -m unittest discover -s tests -v
```
