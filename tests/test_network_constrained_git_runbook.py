from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "development" / "network-constrained-git.md"


def test_offline_runbook_pins_bundled_uv_first_in_path() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    offline_section = text.split("For offline execution", maxsplit=1)[1].split(
        "## 3. Build one local expected tree", maxsplit=1
    )[0]

    assert 'export PATH="$toolchain_dir:$PATH"' in offline_section
    assert '"$toolchain_dir/uv" run --frozen --offline python scripts/quality.py' in offline_section
    assert (
        "every `uv` subprocess in the quality gate resolves to that bundled executable"
        in offline_section
    )
