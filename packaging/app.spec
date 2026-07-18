# PyInstaller spec: freeze the app into one self-contained Linux executable.
#
# Build with packaging/build.sh, which runs PyInstaller in a throwaway venv
# holding only the app's real dependencies. Building from a fatter environment
# works but bloats the binary: optional imports in the dependencies (PIL,
# numpy, ...) pick up whatever happens to be installed.
#
# The result is dist/<package name>. ffmpeg is deliberately not bundled: the
# app invokes it from PATH and users install it themselves.
#
# The package-name literal below is retargeted by publish.sh, like the
# APP_NAME fallback in config.py, so the same spec builds the public binary
# under its own name.

import os
import sys

from PyInstaller.utils.hooks import collect_data_files

APP_NAME = 'ecoesp'

# The spec lives in packaging/; the package itself is one level up. Put the
# repo root on sys.path so collect_data_files can find the package.
ROOT = os.path.dirname(SPECPATH)
sys.path.insert(0, ROOT)

a = Analysis(
    [os.path.join(SPECPATH, 'entry.py')],
    pathex=[ROOT],
    # The shipped prompts and email template are package data; this picks up
    # every non-.py file inside the package, at its package-relative path, so
    # TEMPLATE_DIR's __file__-based resolution works unchanged in the frozen
    # app. googleapiclient's discovery documents are handled by the override
    # hook in hooks/, which beats PyInstaller's bundled collect-everything one.
    datas=collect_data_files(APP_NAME),
    hookspath=[os.path.join(SPECPATH, 'hooks')],
    hiddenimports=[],
    # Insurance against building outside build.sh's clean venv: these are
    # optional imports of the dependencies, not used by this app, and would
    # otherwise ride in from a fat environment.
    excludes=['numpy', 'matplotlib', 'PIL', 'kiwisolver', 'pygments',
              'jedi', 'IPython', 'zmq', 'tkinter'],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name=APP_NAME,
    console=True,
    strip=False,
    upx=False,
)
