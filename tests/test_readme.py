"""Keep the README and the script that tests it on Windows in sync."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_tagged_command_appears_verbatim_in_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "readme_check.ps1").read_text(encoding="utf-8")
    tagged = [
        ln.split("  # README")[0].strip()
        for ln in script.splitlines()
        if ln.rstrip().endswith("# README")
    ]
    assert len(tagged) >= 12
    missing = [cmd for cmd in tagged if cmd not in readme]
    assert not missing, f"README no longer contains: {missing}"


def test_readme_has_no_unfilled_placeholders():
    assert "SAMPLE_" not in (ROOT / "README.md").read_text(encoding="utf-8")


def test_files_the_readme_links_to_exist():
    import re

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\(((?!http|#)[^)]+)\)", readme):
        assert (ROOT / target).exists(), f"README links to missing {target}"
