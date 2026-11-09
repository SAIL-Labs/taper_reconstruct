"""PyInstaller entry script (gui-scripts entry points can't be used directly)."""
import sys

from taper_tools.recorder import main

sys.exit(main())
