"""tools/htop.py parses toybox `top -n 1` output without a TV."""
import unittest

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import htop  # noqa: E402

SAMPLE = """\
\x1b[H\x1b[JTasks: 428 total,   1 running, 427 sleeping,   0 stopped,   0 zombie
  Mem:  3962200K total,  3525720K used,   436480K free,     1264K buffers
 Swap:  2165688K total,   480000K used,  1685688K free,  1335892K cached
400%cpu  12%user   0%nice  18%sys 364%idle   0%iow   3%irq   3%sirq   0%host
\x1b[7m  PID USER         PR  NI VIRT  RES  SHR S[%CPU] %MEM     TIME+ ARGS\x1b[0m
 16858 shell        20   0  19M 4.1M 3.3M R 21.2   0.1   0:00.09 top -n 1 -m 25
   503 audioserver  20   0 204M 125M  12M S  9.0   3.2  53:26.34 android.hardware.audio.service.mediatek
  2151 u0_a128      20   0 2.1G 242M 146M S  3.0   6.2  26:15.68 org.smarttube.beta
  2092 u0_a125      20   0 1.9G 165M  83M S  3.0   4.2  15:59.83 com.spotify.tv.android
   557 system       -3  -8 128M  33M  16M S  3.0   0.8  22:34.79 surfaceflinger
 15139 u0_a41       20   0 1.5G  71M  36M S  0.0   1.8   0:00.11 com.android.providers.tv
 14400 u0_a119      20   0 1.7G  90M  40M S  0.0   2.3   0:01.00 com.cbs.ca
"""


class ParseTopTest(unittest.TestCase):
    def test_strips_ansi_and_reads_rows(self):
        header, rows = htop.parse_top(SAMPLE)
        self.assertTrue(any("Mem:" in h for h in header))
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[1]["user"], "audioserver")
        self.assertEqual(rows[2]["args"], "org.smarttube.beta")
        self.assertAlmostEqual(rows[2]["cpu"], 3.0)
        self.assertAlmostEqual(rows[2]["mem"], 6.2)

    def test_apps_filter_drops_top_keeps_apps_and_audio(self):
        _, rows = htop.parse_top(SAMPLE)
        kept = [r for r in rows if htop.keep_row(r, apps_only=True)]
        names = [r["args"] for r in kept]
        self.assertNotIn("top -n 1 -m 25", names)
        self.assertIn("org.smarttube.beta", names)
        self.assertIn("android.hardware.audio.service.mediatek", names)
        self.assertIn("surfaceflinger", names)

    def test_sort_by_mem(self):
        _, rows = htop.parse_top(SAMPLE)
        ordered = htop.sort_rows(rows, "mem")
        self.assertEqual(ordered[0]["args"], "org.smarttube.beta")


class LabelsAndUnitsTest(unittest.TestCase):
    def test_mem_header_uses_gb_and_mb(self):
        line = "  Mem:  3962200K total,  3525720K used,   436480K free,     1264K buffers"
        out = htop.rewrite_mem_line(line)
        self.assertIn("3.8 GB", out)
        self.assertIn("3.4 GB", out)
        self.assertIn("426 MB", out)  # 436480/1024
        self.assertIn("1.2 MB", out)
        self.assertNotIn("K", out)

    def test_cpu_line_as_percent_of_chip(self):
        line = "400%cpu  12%user   0%nice  18%sys 364%idle   0%iow   3%irq   3%sirq   0%host"
        out = htop.rewrite_cpu_line(line)
        self.assertIn("4 cores", out)
        self.assertIn("3% user", out)   # 12/4
        self.assertIn("91% idle", out)  # 364/4

    def test_friendly_names_for_known_processes(self):
        labels = htop.build_label_map(ROOT)
        self.assertEqual(
            htop.friendly_name("android.hardware.audio.service.mediatek", labels),
            "Audio (MediaTek HAL)",
        )
        self.assertEqual(htop.friendly_name("com.cbs.ca", labels), "Paramount+ / CBS")
        self.assertEqual(htop.friendly_name("org.smarttube.beta", labels), "SmartTube")
        self.assertEqual(htop.friendly_name("com.spotify.tv.android", labels), "Spotify")

    def test_debloat_comments_are_loaded(self):
        labels = htop.load_debloat_labels(ROOT)
        self.assertIn("com.cbs.ca", labels)

    def test_res_human(self):
        self.assertEqual(htop.res_human("125M"), "125 MB")
        self.assertEqual(htop.res_human("2.1G"), "2.1 GB")
        self.assertEqual(htop.res_human("4.1M"), "4.1 MB")

    def test_res_to_kb(self):
        self.assertEqual(htop.res_to_kb("125M"), 128000)
        self.assertEqual(htop.res_to_kb("2.1G"), 2202009)  # int(2.1 * 1024 * 1024)
        self.assertEqual(htop.res_to_kb("4.1M"), 4198)  # int(4.1 * 1024)
        self.assertEqual(htop.res_to_kb("512K"), 512)
        self.assertEqual(htop.res_to_kb("nope"), 0)

    def test_format_log_line(self):
        _, rows = htop.parse_top(SAMPLE)
        smart = next(r for r in rows if r["args"] == "org.smarttube.beta")
        htop.enrich_rows([smart], htop.build_label_map(ROOT))
        line = htop.format_log_line(1730000000, smart)
        self.assertEqual(
            line,
            "1730000000\torg.smarttube.beta\tSmartTube\t3.0\t6.2\t242.0\n",
        )

    def test_format_log_header(self):
        text = htop.format_log_header("192.168.1.50", 1.0, True)
        self.assertIn("# cleanyourtv-htop-log v2\n", text)
        self.assertIn("host=192.168.1.50 interval=1.0 apps=1", text)
        self.assertTrue(text.endswith("t\tproc\tname\tcpu\tmem\tres_mb\n"))

    def test_render_includes_name_column(self):
        header, rows = htop.parse_top(SAMPLE)
        rows = [r for r in rows if htop.keep_row(r, apps_only=True)]
        htop.enrich_rows(rows, htop.build_label_map(ROOT))
        frame = htop.render("192.168.1.50", header, rows, 10, "cpu")
        self.assertIn("NAME", frame)
        self.assertIn("Paramount+ / CBS", frame)
        self.assertIn("3.8 GB", frame)


if __name__ == "__main__":
    unittest.main()
