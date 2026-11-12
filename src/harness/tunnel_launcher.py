"""Entry point copied beside the root/admin-owned isolated runtime package."""

import os
import sys
from pathlib import Path

# -I -S disables user site/env/PYTHONPATH. Only the installer's immutable package
# directory is added, not the checkout or the invoking user's virtualenv.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if os.name == "nt":
    site = Path(sys.base_prefix) / "Lib/site-packages"
    sys.path.extend(str(path) for path in (site, site / "win32", site / "win32/lib"))
    if (site / "pywin32_system32").is_dir():
        _dll_directory = os.add_dll_directory(str(site / "pywin32_system32"))  # type: ignore[attr-defined]

from harness.tunnel_service import main

raise SystemExit(main())
