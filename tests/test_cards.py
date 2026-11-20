from datetime import date, timedelta
import json
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import render_cards as cards


class ContributionTests(unittest.TestCase):
    def days(self, counts, start=date(2026, 1, 29)):
        return [{"date": (start + timedelta(days=i)).isoformat(), "count": count}
                for i, count in enumerate(counts)]

    def test_streak_can_continue_from_yesterday(self):
        stats = cards.statistics(self.days([0, 2, 1, 4, 0]))
        self.assertEqual((stats["total"], stats["active"], stats["streak"], stats["longest"]), (7, 3, 3, 3))
        self.assertEqual(stats["busiest"]["count"], 4)

    def test_two_empty_days_end_streak(self):
        self.assertEqual(cards.statistics(self.days([3, 3, 0, 0]))["streak"], 0)

    def test_today_counts_towards_streak(self):
        self.assertEqual(cards.statistics(self.days([0, 1, 1]))["streak"], 2)

    def test_empty_calendar_has_no_busiest_day(self):
        stats = cards.statistics(self.days([0, 0, 0]))
        self.assertIsNone(stats["busiest"])
        self.assertEqual(stats["streak"], 0)

    def test_boundary_months_and_earliest_busiest_tie(self):
        stats = cards.statistics(self.days([2, 0, 2, 1]))
        self.assertEqual(stats["monthly"], {"2026-01": 4, "2026-02": 1})
        self.assertEqual(stats["busiest"]["date"], "2026-01-29")

    def test_snapshot_validation_rejects_gaps_duplicates_and_bad_counts(self):
        data = cards.read_json(cards.ROOT / "data/contributions.json")
        cards.validate_data(data, data["username"])
        for mutate in (
            lambda value: value["days"].pop(),
            lambda value: value["days"][1].update(date=value["days"][0]["date"]),
            lambda value: value["days"][0].update(count=-1),
            lambda value: value["days"][0].update(count=True),
        ):
            value = json.loads(json.dumps(data))
            mutate(value)
            with self.assertRaises(ValueError):
                cards.validate_data(value, data["username"])

    def test_public_calendar_preserves_anonymous_counts_and_zeros(self):
        parser = cards.CalendarParser()
        parser.feed('<td id="a" data-date="2025-09-01"></td><tool-tip for="a">12 contributions on September 1st.</tool-tip><td id="b" data-date="2025-09-02"></td><tool-tip for="b">No contributions on September 2nd.</tool-tip><td id="c" data-date="2025-09-03"></td><tool-tip for="c">1,234 contributions on September 3rd.</tool-tip>')
        self.assertEqual(parser.days(), [{"date": "2025-09-01", "count": 12},
                                         {"date": "2025-09-02", "count": 0},
                                         {"date": "2025-09-03", "count": 1234}])

    def test_excluded_pins_do_not_reappear(self):
        profile = {"excluded_repos": ["chart"]}
        data = {"featured_repos": [{"name": "Chart", "full_name": "user/Chart"},
                                   {"name": "keep", "full_name": "user/keep"}]}
        self.assertEqual([repo["name"] for repo in cards.featured_repos(profile, data)], ["keep"])

    def test_leap_day_is_preserved(self):
        days = self.days([1, 2, 3], date(2024, 2, 28))
        self.assertEqual(days[1]["date"], "2024-02-29")
        self.assertEqual(cards.statistics(days)["monthly"], {"2024-02": 3, "2024-03": 3})


class SvgTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = cards.read_json(cards.ROOT / "profile.json")
        cls.theme = cls.profile["theme"]

    def test_static_removes_smil_and_disables_css_motion(self):
        hero = cards.hero(self.profile, self.theme)
        with patch.object(cards, "subset_font", return_value="d09GRg=="):
            animated = cards.svg(hero, self.theme, False)
            static = cards.svg(hero, self.theme, True)
        root = ET.fromstring(static)
        self.assertEqual(root.findall(f".//{{{cards.SVG_NS}}}animate"), [])
        self.assertIn("animation:none!important", static)
        self.assertIn("prefers-reduced-motion:reduce", animated)
        self.assertIn("stroke-dashoffset:0!important", animated)
        self.assertIn("data:font/woff;base64,", animated)
        self.assertNotIn("foreignObject", animated)

    def test_text_is_xml_escaped(self):
        markup = cards.text(0, 20, '<script>& "text"')
        self.assertEqual(ET.fromstring(markup).text, '<script>& "text"')

    def test_heatmap_uses_brand_accent_and_zero_is_empty(self):
        self.assertEqual(cards.heat_color(0, 9, self.theme), self.theme["surface"])
        self.assertEqual(cards.heat_color(9, 9, self.theme), self.theme["contribution"])

    def test_readme_contains_three_separately_linked_tiles(self):
        hero = cards.hero(self.profile, self.theme)
        tiles = cards.tiles(self.profile)
        markup = cards.readme([hero, *tiles])
        self.assertEqual(markup.count('width="32.5%"'), 3)
        for tile in tiles:
            self.assertIn(f'<a href="{tile.href.replace("&", "&amp;")}"><img', markup)

    def test_long_descriptions_wrap_without_losing_words(self):
        value = "A long description " * 30 + "x" * 120
        lines = cards.wrap_text(value, 650, 16)
        self.assertGreater(len(lines), 2)
        self.assertEqual("".join("".join(lines).split()), "".join(value.split()))


if __name__ == "__main__":
    unittest.main()
