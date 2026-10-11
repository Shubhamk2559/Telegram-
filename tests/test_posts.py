import re
import tempfile
import unittest
from pathlib import Path

from src import posts as P

U = lambda i: f"https://www.diskwala.com/app/video{i}"
T = "https://telegra.ph/example-article"


def plan(n, **kw):
    kw.setdefault("telegraph_urls", [T])
    return P.plan_posts(video_links=[U(i) for i in range(1, n + 1)], **kw)


class Sorting(unittest.TestCase):
    def test_largest_first_with_deterministic_tiebreak(self):
        vs = [{"filename": "D.mp4", "size": 200}, {"filename": "b.mp4", "size": 450}, {"filename": "A.mp4", "size": 900},
              {"filename": "c.mp4", "size": 450}, {"filename": "B.mp4", "size": 450}]
        self.assertEqual([v["filename"] for v in P.sort_videos(vs)], ["A.mp4", "B.mp4", "b.mp4", "c.mp4", "D.mp4"])
        self.assertEqual(P.sort_videos(vs), P.sort_videos(list(reversed(vs))))  # input order se farq nahi

    def test_actual_size_from_disk_not_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a").mkdir(); (Path(d) / "a/x.mp4").write_bytes(b"1" * 7)
            out = P.with_actual_sizes([{"filename": "x.mp4", "path": "a/x.mp4", "size": 999}], Path(d))
            self.assertEqual(out[0]["size"], 7)
            with self.assertRaises(P.PostError):
                P.with_actual_sizes([{"filename": "m", "path": "a/missing.mp4", "size": 1}], Path(d))
            with self.assertRaises(P.PostError):
                P.with_actual_sizes([{"filename": "m", "path": "../x", "size": 1}], Path(d))


class Splitting(unittest.TestCase):
    def test_counts_for_1_8_9_16_17_25_33(self):
        for n, posts in [(1, 1), (8, 1), (9, 2), (16, 2), (17, 3), (25, 4), (33, 5)]:
            ps = plan(n)
            self.assertEqual(len(ps), posts, n)
            self.assertTrue(all(1 <= p.link_count <= 8 for p in ps))
            # order poori tarah preserve: har post me links start se count tak, contiguous
            self.assertEqual([p.link_start for p in ps], list(range(0, n, 8)))
            seen = [u for p in ps for u in [U(i) for i in range(1, n + 1)][p.link_start:p.link_start + p.link_count]]
            self.assertEqual(seen, [U(i) for i in range(1, n + 1)])
            for p in ps:  # link text me exactly utne links aur sahi order me
                found = re.findall(r"https://www\.diskwala\.com/app/video\d+", p.text)
                self.assertEqual(found, [U(i) for i in range(p.link_start + 1, p.link_start + p.link_count + 1)])

    def test_same_telegraph_url_in_every_post(self):
        for p in plan(20):
            self.assertEqual(p.text.count(T), 1); self.assertTrue(p.text.startswith(f"Articles Images {T}"))

    def test_exact_expected_output_for_10_videos(self):
        p1, p2 = plan(10)
        self.assertEqual(p1.text, f"Articles Images {T}\n\nVideo links " + "\n\n".join(U(i) for i in range(1, 9)))
        self.assertEqual(p2.text, f"Articles Images {T}\n\nVideo links {U(9)}\n\n{U(10)}")

    def test_exactly_one_blank_line_between_links(self):
        text = plan(5)[0].text
        self.assertNotIn("\n\n\n", text)
        self.assertEqual(P.format_links(["a", "b", "c"]), "a\n\nb\n\nc")
        self.assertEqual(P.format_links(["a"]), "a")

    def test_custom_max_links(self):
        self.assertEqual(len(plan(10, max_links=3)), 4)
        with self.assertRaises(P.PostError):
            plan(3, max_links=0)

    def test_length_limit_shrinks_post_but_keeps_order(self):
        links = [f"https://x.example/{'a' * 100}{i}" for i in range(8)]
        ps = P.plan_posts(video_links=links, telegraph_urls=[T], max_chars=400)
        self.assertGreater(len(ps), 1)
        self.assertTrue(all(P.utf16_len(p.text) <= 400 for p in ps))
        self.assertEqual([p.link_start for p in ps], [sum(q.link_count for q in ps[:i]) for i in range(len(ps))])
        self.assertEqual(sum(p.link_count for p in ps), 8)
        with self.assertRaises(P.PostError):
            P.plan_posts(video_links=links, telegraph_urls=[T], max_chars=50)

    def test_emoji_counted_in_utf16(self):
        self.assertEqual(P.utf16_len("a😀"), 3)


class Templates(unittest.TestCase):
    def test_escape_sequences(self):
        self.assertEqual(P.unescape_template(r"A {telegraph_url}\n\nB {video_links}"), "A {telegraph_url}\n\nB {video_links}")
        self.assertEqual(P.unescape_template(r"a\tb\\n"), "a\tb\\n")

    def test_custom_template_unicode_emoji(self):
        tpl = P.unescape_template(r"🎬 Gallery: {telegraph_url}\n\n⬇️ Links:\n{video_links}")
        text = plan(2, template=tpl)[0].text
        self.assertTrue(text.startswith(f"🎬 Gallery: {T}\n\n⬇️ Links:\n{U(1)}\n\n{U(2)}"))

    def test_unknown_and_unsafe_variables_rejected(self):
        for bad in ("{title} {video_links}", "{video_links.__class__}", "{video_links[0]}", "{0} {video_links}",
                    "{video_links:>10}", "{video_links!r}", "{ video_links }", "{video_links", "{}"):
            with self.assertRaises(P.TemplateError, msg=bad):
                plan(1, template=bad)

    def test_literal_braces_ok_and_video_links_required(self):
        self.assertIn("{x}", plan(1, template="{{x}} {video_links}")[0].text)
        with self.assertRaises(P.TemplateError):
            plan(1, template="only {telegraph_url}")

    def test_telegraph_urls_variable_and_first_page_default(self):
        pages = ["https://telegra.ph/p1", "https://telegra.ph/p2"]
        t = plan(1, telegraph_urls=pages, template="{telegraph_url}|{telegraph_urls}|{video_links}")[0].text
        self.assertEqual(t, f"https://telegra.ph/p1|https://telegra.ph/p1\nhttps://telegra.ph/p2|{U(1)}")


class Policies(unittest.TestCase):
    def test_images_only_single_post(self):
        ps = P.plan_posts(video_links=[], telegraph_urls=[T])
        self.assertEqual([(p.kind, p.text) for p in ps], [("images_only", f"Articles Images {T}")])

    def test_videos_only_uses_videos_template_and_splits(self):
        ps = P.plan_posts(video_links=[U(i) for i in range(1, 10)], telegraph_urls=[])
        self.assertEqual(len(ps), 2); self.assertEqual(ps[0].kind, "videos_only")
        self.assertTrue(ps[1].text.startswith("Video links ") and "Articles" not in ps[1].text)

    def test_no_empty_placeholders(self):
        with self.assertRaises(P.PostError):
            P.plan_posts(video_links=[], telegraph_urls=[])

    def test_invalid_urls_rejected(self):
        with self.assertRaises(P.PostError):
            P.plan_posts(video_links=["not a url"], telegraph_urls=[T])
        with self.assertRaises(P.PostError):
            P.plan_posts(video_links=[U(1)], telegraph_urls=["javascript:alert(1)"])

    def test_zip_isolation_links_never_mix(self):
        a = P.plan_posts(video_links=["https://d/a1", "https://d/a2"], telegraph_urls=["https://telegra.ph/A"])
        b = P.plan_posts(video_links=["https://d/b1"], telegraph_urls=["https://telegra.ph/B"])
        self.assertNotIn("b1", a[0].text); self.assertNotIn("/A", b[0].text)


if __name__ == "__main__":
    unittest.main()
