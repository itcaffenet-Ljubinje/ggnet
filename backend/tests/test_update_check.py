"""Unfinished Windows updates in OS image versions (app.windows_setup.update_signs, app.services.update_check)."""

from __future__ import annotations

from pathlib import Path

from app.services.update_check import CHECKING, NONE, PENDING, UNKNOWN, UpdateChecker
from app.windows_setup import WindowsSetup

CBS = "\\Microsoft\\Windows\\CurrentVersion\\Component Based Servicing"
AU = "\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update"


class HiveRunner:
    """Answers the hive_children() perl script from a dict of key -> subkeys (None: unreadable)."""

    def __init__(self, keys: dict[str, list[str] | None]):
        self.keys, self.calls = keys, []

    def run(self, cmd, quiet=False, **kw):
        self.calls.append(cmd)
        assert cmd[0] == "perl"
        children = self.keys.get(cmd[-1], [])
        if children is None:
            return False, "", "hivex_open: failed"
        return True, "\n".join(children), ""


def _windows(root: Path, pending_xml: bool = False) -> Path:
    config = root / "Windows" / "System32" / "config"
    config.mkdir(parents=True)
    (config / "SOFTWARE").write_bytes(b"regf")
    (root / "Windows" / "WinSxS").mkdir()
    if pending_xml:
        (root / "Windows" / "WinSxS" / "pending.xml").write_text("<xml/>")
    return root


def test_a_clean_windows_has_no_signs(tmp_path):
    runner = HiveRunner({CBS: ["Packages", "SessionsPending"], AU: ["Results"]})
    assert WindowsSetup(runner).update_signs(_windows(tmp_path)) == []
    assert len(runner.calls) == 2                        # one look per key, not per sign


def test_an_unfinished_update_shows_its_signs(tmp_path):
    runner = HiveRunner({CBS: ["Packages", "RebootPending", "PackagesPending"], AU: ["RebootRequired"]})
    assert WindowsSetup(runner).update_signs(_windows(tmp_path, pending_xml=True)) == [
        "pending.xml", "RebootPending", "PackagesPending", "RebootRequired"]


def test_an_unreadable_hive_is_not_taken_for_no_update(tmp_path):
    assert "cannot be read" in WindowsSetup(HiveRunner({CBS: None})).update_signs(_windows(tmp_path))
    assert WindowsSetup(HiveRunner({})).update_signs(tmp_path / "empty") == "no SOFTWARE hive in the image"


def test_the_hive_script_reads_subkey_names():
    runner = HiveRunner({CBS: ["Packages", "RebootPending"]})
    assert WindowsSetup(runner).hive_children(Path("/x/SOFTWARE"), CBS) == ["Packages", "RebootPending"]
    assert runner.calls[0][-2:] == ["/x/SOFTWARE", CBS]


# ── Background check of the active versions ──────────────────────────

class FakeProv:
    def __init__(self, results):
        self.results, self.props, self.checked = results, {}, []
        prov = self

        class Zfs:
            def snapshot_update(self, snapshot):
                return prov.props.get(snapshot)

        self.zfs = Zfs()

    def check_update(self, master, snapshot):
        self.checked.append(f"{master}@{snapshot}")
        found = self.results[f"{master}@{snapshot}"]
        if isinstance(found, list):
            self.props[f"{master}@{snapshot}"] = found
        return found


def test_unchecked_versions_are_checked_once_in_the_background():
    prov = FakeProv({"img/a@v2": ["pending.xml"], "img/b@base": [], "img/c@v3": "no Windows partition found"})
    checker = UpdateChecker(prov, background=False)
    assert checker.state("img/a", "v2")[0] == CHECKING
    assert checker.state("img/a", "v2")[0] == CHECKING          # queued once
    checker.state("img/b", "base")
    checker.state("img/c", "v3")
    checker.run()
    assert prov.checked == ["img/a@v2", "img/b@base", "img/c@v3"]
    assert checker.state("img/a", "v2") == (PENDING, ["pending.xml"], None)
    assert checker.state("img/b", "base") == (NONE, [], None)
    assert checker.state("img/c", "v3") == (UNKNOWN, [], "no Windows partition found")
    checker.run()
    assert prov.checked == ["img/a@v2", "img/b@base", "img/c@v3"]  # not again
