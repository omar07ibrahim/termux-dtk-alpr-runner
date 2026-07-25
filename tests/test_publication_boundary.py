from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RepositoryPublicationBoundaryTests(unittest.TestCase):
    def test_readme_contains_no_personal_host_paths_or_inline_rtsp_credentials(
        self,
    ) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertNotRegex(readme, re.compile(r"/Users/[^/\s]+/"))
        self.assertNotRegex(readme, re.compile(r"rtsp://[^/\s]*:[^@/\s]+@"))
        self.assertNotIn("DTK system id:", readme)

    def test_runtime_status_code_uses_redacted_source_descriptors(self) -> None:
        app = (ROOT / "alpr_runner/app.py").read_text(encoding="utf-8")
        ffmpeg = (ROOT / "alpr_runner/ffmpeg_video.py").read_text(encoding="utf-8")
        multi = (ROOT / "alpr_runner/multi_video.py").read_text(encoding="utf-8")

        self.assertNotIn('"source": str(frame_path)', app)
        self.assertNotIn('"source": self.args.rtsp', ffmpeg)
        self.assertNotIn('"dtk_dir": str(self.dtk_dir)', multi)
        self.assertNotIn('print(f"DTK system id:', app)
        self.assertIn("source_descriptor(", app)
        self.assertIn("source_descriptor(", ffmpeg)
        self.assertIn("source_descriptor(", multi)
