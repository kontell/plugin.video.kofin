import os
import sys

_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from kofin.plugin.context import browse_extras  # noqa: E402

if __name__ == "__main__":
    browse_extras()
