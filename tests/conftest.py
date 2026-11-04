import sys
from pathlib import Path

# The modules live at the repo root, not in a package. Put the root on the
# path so `import nightcore` works without an install step.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
