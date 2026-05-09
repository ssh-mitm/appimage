import datetime

project = "appimage"
author = "Manfred Kaiser"
copyright = f"{datetime.datetime.now().year}, {author}"  # pylint: disable=redefined-builtin

extensions = [
    "sphinx_copybutton",
    "sphinx.ext.autodoc",
    "myst_parser",
]

myst_enable_extensions = ["colon_fence"]

html_theme = "sphinx_rtd_theme"
html_logo = "_static/appimage.png"

html_theme_options = {
    "logo_only": False,
    "navigation_depth": 3,
}

html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_baseurl = "https://appimage.readthedocs.io/"

master_doc = "index"
autosectionlabel_maxdepth = 1

copybutton_prompt_text = r"\$ |> "
copybutton_prompt_is_regexp = True
copybutton_only_copy_prompt_lines = True
copybutton_selector = "div:not(.no-copybutton) > div.highlight > pre"
copybutton_line_continuation_character = "\\"

language = "en"
exclude_patterns = []
