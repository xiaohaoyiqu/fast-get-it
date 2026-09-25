"""Keep Requests on Python's stdlib JSON decoder in frozen builds.

An incomplete ``simplejson`` namespace can be left behind when a frozen app is
updated by overlaying files. Requests treats its presence as a complete package
and then fails to import ``JSONDecodeError``. The application does not depend on
simplejson, so make Requests use the compatible stdlib implementation.
"""

import sys


sys.modules["simplejson"] = None
