"""Check that the prebuilt website in the package matches its source and carries no site-specific names."""

import base64
import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "nanohpc" / "website-source"
BUILT = ROOT / "src" / "nanohpc" / "website"
HASH_FILE = BUILT / "build-source.sha256"

# Top-level source files and folders whose content goes into the build. The source folder's
# scripts/hash-source.mjs computes the same hash during `npm run build`; keep both in step.
SOURCE_FILES = ("index.html", "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts")
SOURCE_FOLDERS = ("src", "public")
IGNORED_NAMES = {".DS_Store"}

# Site names from the deployment this website was ported from, base64-encoded so the names themselves do
# not appear in this repository's code. Each entry: (encoded name, match ignoring case).
FORBIDDEN = (
    ("U0xVUk0tUkVBTA==", True),
    ("UkVBTA==", False),
    ("SVRV", False),
    ("cmVhbC1pdHU=", True),
    ("dmFwb3I=", True),
    ("cmVhbDM=", True),
    ("aXR1LmRr", True),
)
TEXT_SUFFIXES = {".html", ".js", ".mjs", ".ts", ".tsx", ".css", ".md", ".json", ".svg", ".sh", ".txt"}


def source_paths() -> list[str]:
    """List the website source files as paths relative to the source folder, sorted."""
    paths = list(SOURCE_FILES)
    for folder in SOURCE_FOLDERS:
        paths.extend(
            path.relative_to(SOURCE).as_posix()
            for path in (SOURCE / folder).rglob("*")
            if path.is_file() and path.name not in IGNORED_NAMES
        )
    return sorted(paths)


def source_hash() -> str:
    """Hash each source file's relative path, byte length, and content, in sorted path order."""
    digest = hashlib.sha256()
    for path in source_paths():
        content = (SOURCE / path).read_bytes()
        digest.update(f"{path}\0{len(content)}\0".encode())
        digest.update(content)
    return digest.hexdigest()


def forbidden_patterns() -> list[re.Pattern[str]]:
    """Whole-word patterns for the forbidden site names."""
    patterns = []
    for encoded, ignore_case in FORBIDDEN:
        name = re.escape(base64.b64decode(encoded).decode())
        patterns.append(re.compile(rf"(?<![A-Za-z0-9]){name}(?![A-Za-z0-9])", re.IGNORECASE if ignore_case else 0))
    return patterns


def built_text_files() -> list[Path]:
    """The built files that are text, such as HTML, JavaScript, CSS, and Markdown."""
    return sorted(path for path in BUILT.rglob("*") if path.is_file() and path.suffix in TEXT_SUFFIXES)


class WebsiteBuildTest(unittest.TestCase):
    def test_build_matches_source(self) -> None:
        """The committed build was made from the current website source."""
        self.assertTrue(HASH_FILE.is_file(), "run npm run build in src/nanohpc/website-source/")
        recorded = HASH_FILE.read_text(encoding="utf-8").strip()
        self.assertEqual(recorded, source_hash(), "run npm run build in src/nanohpc/website-source/")

    def test_build_has_page_and_templates(self) -> None:
        """The build has the page and the docs.md and policy.md templates for the deploy to fill in."""
        for name in ("index.html", "docs.md", "policy.md"):
            self.assertTrue(
                (BUILT / name).is_file(), f"{name} is missing; run npm run build in src/nanohpc/website-source/"
            )
        docs = (BUILT / "docs.md").read_text(encoding="utf-8")
        for placeholder in ("cluster_name", "login_address", "home_quota_soft_gb", "home_quota_hard_gb"):
            self.assertIn("{{" + placeholder + "}}", docs)
        allowed = {"cluster_name", "login_address", "home_quota_soft_gb", "home_quota_hard_gb", "scratch_cleanup_days"}
        for name in ("docs.md", "policy.md"):
            used = set(re.findall(r"\{\{(\w+)\}\}", (BUILT / name).read_text(encoding="utf-8")))
            self.assertLessEqual(used, allowed, f"{name} uses unknown placeholders")

    def test_no_site_names(self) -> None:
        """No website source or built file names the deployment the website was ported from."""
        built = built_text_files()
        self.assertTrue(built, "no built files; run npm run build in src/nanohpc/website-source/")
        sources = [SOURCE / path for path in source_paths() if Path(path).suffix in TEXT_SUFFIXES]
        sources += [
            path
            for folder in ("scripts", "tests")
            for path in (SOURCE / folder).rglob("*")
            if path.is_file() and path.suffix in TEXT_SUFFIXES
        ]
        for path in sources + built:
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden_patterns():
                for match in pattern.finditer(text):
                    # Bundled libraries use some of these words as property names, such as p5's font parser's
                    # `.REAL`; a name after a dot in built JavaScript is library code, not site text.
                    if path in built and path.suffix == ".js" and text[match.start() - 1] == ".":
                        continue
                    self.fail(
                        f"{path.relative_to(ROOT)} contains a site-specific name: {text[match.start() - 30 : match.end() + 30]!r}"
                    )

    def test_relative_urls(self) -> None:
        """The build works under any path: no absolute /cluster/ URLs, and the page loads assets relatively."""
        for path in built_text_files():
            self.assertNotIn("/cluster/", path.read_text(encoding="utf-8"), f"{path.relative_to(ROOT)}")
        page = (BUILT / "index.html").read_text(encoding="utf-8")
        for url in re.findall(r'(?:src|href)="([^"]*)"', page):
            self.assertFalse(url.startswith("/"), f"index.html has the absolute URL {url}")


if __name__ == "__main__":
    unittest.main()
