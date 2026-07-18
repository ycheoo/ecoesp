"""PyInstaller entry point.

The normal launch path, `python -m ecoesp`, enters the package through
runpy, which PyInstaller cannot use as an analysis root; this shim gives it a
plain script that reaches the same main() through an absolute import. The
package-name literal is retargeted by publish.sh, like the APP_NAME fallback
in config.py.
"""

import sys

from ecoesp.__main__ import main

if __name__ == '__main__':
    sys.exit(main())
