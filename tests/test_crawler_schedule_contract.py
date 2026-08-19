from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_official_registry_is_weekly_while_news_and_community_are_daily():
    deploy = (ROOT / "scripts/deploy_autoslice.sh").read_text(encoding="utf-8")

    assert "streamer_registry_cron='7 6 * * 0 " in deploy
    assert "psplive_roster_cron='12 6 * * 0 " in deploy
    assert "timely_terms_cron='17 6 * * * " in deploy
    assert "community_names_cron='27 6 * * * " in deploy
    assert "topic_entity_cron='37 6 * * * " in deploy
    assert "streamer_dynamics_cron='47 6 * * * " in deploy
    assert "source /opt/bilive/autoslice/cpa.env" in deploy
    for script in (
        "crawl_streamer_registry.py",
        "crawl_psplive_roster.py",
        "crawl_timely_terms.py",
        "crawl_community_names.py",
        "crawl_topic_entity_graph.py",
        "crawl_streamer_dynamics.py",
    ):
        assert f"grep -Fv 'scripts/{script}'" in deploy


def test_new_crawler_entrypoints_and_deploy_shell_parse():
    for script in (
        "crawl_streamer_registry.py",
        "crawl_community_names.py",
        "crawl_streamer_dynamics.py",
    ):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
    completed = subprocess.run(
        ["bash", "-n", str(ROOT / "scripts/deploy_autoslice.sh")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_oss_export_refuses_to_replace_a_git_checkout(tmp_path):
    exporter = ROOT / "scripts/export_oss_snapshot.py"
    if not exporter.is_file():
        pytest.skip("private snapshot exporter is intentionally absent from OSS")
    target = tmp_path / "public-checkout"
    (target / ".git").mkdir(parents=True)
    sentinel = target / "keep.txt"
    sentinel.write_text("must survive", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(exporter), str(target)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert ".git" in completed.stdout
    assert sentinel.read_text(encoding="utf-8") == "must survive"
