"""scripts/break_test.py must refuse to mutate anything but a disposable temporary copy."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "break_test.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("break_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["break_test"] = module
    spec.loader.exec_module(module)
    return module


def fake_copy(base: Path) -> Path:
    (base / "app" / "ledger").mkdir(parents=True)
    (base / "tests").mkdir()
    return base


def test_refuses_the_real_backend() -> None:
    bt = load()
    with pytest.raises(bt.UnsafeWorkdir):
        bt.ensure_disposable_copy(BACKEND)


def test_refuses_a_directory_outside_the_temp_root(tmp_path: Path) -> None:
    bt = load()
    copy = fake_copy(tmp_path / "copy")
    with pytest.raises(bt.UnsafeWorkdir, match="temporary directory"):
        bt.ensure_disposable_copy(copy, temp_root=tmp_path / "elsewhere")


def test_refuses_the_temp_root_itself(tmp_path: Path) -> None:
    bt = load()
    fake_copy(tmp_path)
    with pytest.raises(bt.UnsafeWorkdir):
        bt.ensure_disposable_copy(tmp_path, temp_root=tmp_path)


def test_refuses_a_git_checkout_even_inside_temp(tmp_path: Path) -> None:
    bt = load()
    (tmp_path / "clone" / ".git").mkdir(parents=True)
    copy = fake_copy(tmp_path / "clone" / "backend")
    with pytest.raises(bt.UnsafeWorkdir, match="git checkout"):
        bt.ensure_disposable_copy(copy, temp_root=tmp_path)


def test_refuses_a_directory_that_is_not_a_backend_copy(tmp_path: Path) -> None:
    bt = load()
    (tmp_path / "empty").mkdir()
    with pytest.raises(bt.UnsafeWorkdir, match="does not look like"):
        bt.ensure_disposable_copy(tmp_path / "empty", temp_root=tmp_path)


def test_accepts_a_temporary_copy(tmp_path: Path) -> None:
    bt = load()
    copy = fake_copy(tmp_path / "copy")
    assert bt.ensure_disposable_copy(copy, temp_root=tmp_path) == copy.resolve()


def test_script_exits_refused_from_the_repo() -> None:
    """End to end: started from backend/, the script stops before touching any file."""
    before = {p: p.read_bytes() for p in (BACKEND / "app" / "ledger").glob("*.py")}
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT)], cwd=BACKEND, capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "REFUSED" in result.stderr
    assert {p: p.read_bytes() for p in before} == before
