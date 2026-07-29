import os
import sys

_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from kofin.plugin.audiotracks import pick  # noqa: E402
from kofin.plugin.router import Request  # noqa: E402

if __name__ == "__main__":
    # Context scripts have no plugin handle/argv mode; synthetic request.
    pick(Request(base_url="", handle=-1, params={}))
