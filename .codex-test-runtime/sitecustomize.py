import sys


PROJECT_VENV_SITE_PACKAGES = (
    r"C:\Users\sagnik\Desktop\rp46-3\venv\Lib\site-packages"
)

if PROJECT_VENV_SITE_PACKAGES not in sys.path:
    # Keep the bundled Python 3.12 runtime's own compiled packages ahead of
    # the project's Python 3.13 wheels; use the project venv only to fill
    # missing pure-Python dependencies while the sandbox blocks its launcher.
    sys.path.append(PROJECT_VENV_SITE_PACKAGES)
