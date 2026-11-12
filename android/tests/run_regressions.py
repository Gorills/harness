#!/usr/bin/env python3
"""Run production service/back control flow against deterministic host platform stubs.

No Android runtime, network, native core or signing material is used. These fixtures
protect scheduling/selection/API gating; they do not prove device interactions.
"""

import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    production = root.parent / "src/com/harness/tunnel"
    sources = sorted((root / "fixtures").rglob("*.java"))
    sources.extend(production / name for name in ("TunnelService.java", "BackNavigation.java"))
    with tempfile.TemporaryDirectory(prefix="harness-android-regressions-") as directory:
        subprocess.run(["javac", "--release", "8", "-d", directory, *map(str, sources)], check=True)
        for fixture in ("ServiceRegressionFixture", "BackNavigationFixture"):
            subprocess.run(["java", "-cp", directory, f"com.harness.tunnel.{fixture}"], check=True)


if __name__ == "__main__":
    main()
