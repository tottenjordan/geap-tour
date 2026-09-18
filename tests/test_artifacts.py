"""A truncated manifest turns a recoverable kill into leaked cloud engines.

`run_bakeoff` deploys two persistent Agent Engines and records their resource
names in `manifest.json`. That file is the **only** record of what was created —
`.env` holds one coordinator id, not these two — so teardown reads it back.

The write was `open(path, "w")` + `json.dump`. `open(..., "w")` truncates
immediately, so a kill between truncate and flush leaves a half-written file. The
engine ids are then gone, and two engines bill until somebody hunts them down by
resource name in the console. Ctrl-C during a bake-off is not hypothetical; it is
the normal way you stop one.

`write_json_atomic` writes a temp file beside the target and `os.replace`s it,
which is atomic within a filesystem on POSIX. The reader sees either the old file
or the new one, never a prefix of the new one.
"""

from __future__ import annotations

import json

import pytest

from src.eval.artifacts import write_json_atomic


class TestAKillMidWriteCannotDestroyThePreviousFile:
    def test_a_failed_write_leaves_the_old_content_intact(self, tmp_path):
        """THE property. With open(...,'w') the old manifest is already gone by the
        time the failure happens — the engine ids with it."""
        target = tmp_path / "manifest.json"
        write_json_atomic(target, {"engines": ["engine-a", "engine-b"]})

        def _explode(_obj):
            raise RuntimeError("killed mid-serialize")

        with pytest.raises(RuntimeError):
            write_json_atomic(target, {"engines": ["engine-c"]}, _serialize=_explode)

        assert json.loads(target.read_text())["engines"] == ["engine-a", "engine-b"]

    def test_no_temp_file_is_left_behind_on_failure(self, tmp_path):
        """A litter of manifest.json.*.tmp next to the real one is its own
        confusion during an incident."""
        target = tmp_path / "manifest.json"
        write_json_atomic(target, {"a": 1})
        with pytest.raises(RuntimeError):
            write_json_atomic(
                target, {"a": 2}, _serialize=lambda _o: (_ for _ in ()).throw(RuntimeError())
            )
        assert [p.name for p in tmp_path.iterdir()] == ["manifest.json"]

    def test_the_temp_file_is_in_the_same_directory(self, tmp_path):
        """os.replace is only atomic within a filesystem. A temp in /tmp would
        silently degrade to a copy across a mount boundary — the exact failure this
        prevents, reintroduced invisibly."""
        import inspect

        from src.eval import artifacts

        src = inspect.getsource(artifacts.write_json_atomic)
        assert "dir=" in src, "the temp file must be created beside the target"


class TestItStillWritesCorrectly:
    def test_a_successful_write_round_trips(self, tmp_path):
        target = tmp_path / "m.json"
        payload = {"engines": ["a"], "nested": {"n": 1}}
        write_json_atomic(target, payload)
        assert json.loads(target.read_text()) == payload

    def test_it_creates_missing_parent_directories(self, tmp_path):
        target = tmp_path / "runs" / "exp1" / "manifest.json"
        write_json_atomic(target, {"ok": True})
        assert target.is_file()

    def test_non_serialisable_values_do_not_abort_the_run(self, tmp_path):
        """The manifests carry timestamps and engine objects; `default=str` matches
        what the call sites already passed to json.dump."""
        from datetime import datetime

        target = tmp_path / "m.json"
        write_json_atomic(target, {"when": datetime(2026, 9, 18)})
        assert "2026-09-18" in target.read_text()

    def test_it_accepts_a_string_path(self, tmp_path):
        """Both call sites build their path with os.path.join, not pathlib."""
        target = tmp_path / "m.json"
        write_json_atomic(str(target), {"ok": True})
        assert target.is_file()


class TestTheManifestWritersUseIt:
    """A helper nothing calls protects nothing."""

    @pytest.mark.parametrize("module", ["src/doe/run_bakeoff.py", "src/doe/launch.py"])
    def test_the_manifest_write_is_atomic(self, module):
        import pathlib

        src = (pathlib.Path(__file__).resolve().parents[1] / module).read_text()
        assert "write_json_atomic" in src, f"{module} still writes its manifest directly"
        assert "json.dump(manifest" not in src, f"{module} has a non-atomic manifest write"
