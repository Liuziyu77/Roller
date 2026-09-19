import os
from pathlib import Path

project = "Roller"
author = "Roller contributors"
copyright = "2026, Roller contributors"

# "en" (default public site) or "zh" (Chinese tree under /zh/).
DOCS_LANG = os.environ.get("DOCS_LANG", "en")


def _version() -> str:
    version_file = Path(__file__).resolve().parents[1] / "src" / "roller" / "version.py"
    for line in version_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return "0.2.0"


release = _version()
version = release

extensions = [
    "myst_parser",
]

myst_enable_extensions = [
    "colon_fence",
    "deflist",
]
myst_heading_anchors = 3

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

master_doc = "index"
language = "zh_CN" if DOCS_LANG == "zh" else "en"

exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    "README.md",
    "requirements-docs.txt",
]

templates_path: list[str] = []
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_js_files = ["ui-locales.js", "language-switcher.js", "sidebar-scroll.js"]

html_theme = "sphinx_rtd_theme"
html_theme_options = {
    # Keep EN/ZH identical: pages expand once to show second-level chapters.
    # collapse_navigation=False avoids the sidebar shrinking when switching pages.
    "navigation_depth": 2,
    "collapse_navigation": False,
    "sticky_navigation": False,
    "style_external_links": True,
}
html_show_sourcelink = False
html_copy_source = False
html_show_sphinx = False

suppress_warnings = ["myst.header"]

# Keep old flat URLs working after files moved into topic folders.
_HTML_REDIRECTS = {
    "getting-started.html": "start/getting-started.html",
    "examples.html": "start/examples.html",
    "cli-reference.html": "start/cli-reference.html",
    "openclaw-configuration.html": "harnesses/openclaw-configuration.html",
    "codex-configuration.html": "harnesses/codex-configuration.html",
    "claude-code-configuration.html": "harnesses/claude-code-configuration.html",
    "task-format.html": "tasks/task-format.html",
    "task-bundle-and-storage.html": "tasks/task-bundle-and-storage.html",
    "user-simulator.html": "tasks/user-simulator.html",
    "trajectory-outputs.html": "traces/trajectory-outputs.html",
    "sft-format.html": "traces/sft-format.html",
    "viewer.html": "traces/viewer.html",
    "bench-format.html": "benches/bench-format.html",
}

_REDIRECT_PAGE = """<!DOCTYPE html>
<html lang="{lang}">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0; url={url}">
  <link rel="canonical" href="{url}">
  <title>Page moved</title>
</head>
<body>
  <p>This page has moved to <a href="{url}">{url}</a>.</p>
</body>
</html>
"""


def setup(app):
    def _write_redirects(app, exception):
        if exception or getattr(app.builder, "name", None) != "html":
            return
        outdir = Path(app.outdir)
        page_lang = "zh-CN" if DOCS_LANG == "zh" else "en"
        for old_name, new_url in _HTML_REDIRECTS.items():
            (outdir / old_name).write_text(
                _REDIRECT_PAGE.format(url=new_url, lang=page_lang),
                encoding="utf-8",
            )

    app.connect("build-finished", _write_redirects)
