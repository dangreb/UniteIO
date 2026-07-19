"""Sphinx configuration for the UniteIO documentation."""

from pathlib import Path
import sys


# Allow a local documentation build without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

project = "UniteIO"
author = "Danilo Greb Santos"
copyright = "2026, Danilo Greb Santos"
version = "0.1"
release = "0.1"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
]

autosummary_generate = True
autodoc_member_order = "bysource"
autodoc_typehints = "description"

napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True

root_doc = "index"
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "sphinx_rtd_theme"
html_static_path = []
