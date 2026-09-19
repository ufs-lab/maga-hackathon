from pathlib import Path
import re

ROOT = Path(__file__).parent.parent


def test_every_relative_link_in_llms_txt_resolves() -> None:
    links = re.findall(r"\]\(([^)]+)\)", (ROOT / "llms.txt").read_text())
    local = [link for link in links if not link.startswith("http")]
    assert local, "llms.txt has no local links, so this test checks nothing"
    assert [link for link in local if not (ROOT / link).exists()] == []


def test_every_maga_command_in_the_readme_is_a_stage_in_the_entry_point() -> None:
    documented = set(re.findall(r"python -m maga (\w+)", (ROOT / "README.md").read_text()))
    source = (ROOT / "src" / "maga" / "__main__.py").read_text()
    stages = set(re.findall(r'add_parser\(\s*"(\w+)"', source))
    assert documented, "the README names no command, so this test checks nothing"
    assert documented <= stages, (
        f"the README names commands that do not exist: {documented - stages}"
    )
