"""Small title-component double for deployment tests that exercise other contracts."""
from pathlib import Path

INSTALL = '''from pathlib import Path
import sys
parent = Path(sys.argv[2]); (parent / "fixture-version").mkdir(parents=True, exist_ok=True)
link = parent / "lidousha-title-cover"
if not link.exists(): link.symlink_to("fixture-version")
'''


def fake_bundle(upload: Path) -> None:
    directory = upload / 'title-skill'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'deploy_oci3_title_skill.py').write_text(INSTALL)


def fake_deploy_source() -> str:
    return ("import os, sys\nfrom pathlib import Path\n"
            "base = os.environ.get('STAGE_TEST_BASE')\n"
            "if base:\n"
            "    directory = Path(base) / ('.oci3-shadow-upload-' + sys.argv[-1]) / 'title-skill'\n"
            "    directory.mkdir(parents=True, exist_ok=True)\n"
            f"    (directory / 'deploy_oci3_title_skill.py').write_text({INSTALL!r})\n")
