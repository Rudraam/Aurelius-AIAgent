"""scripts/break_test.py: it refuses to mutate anything but a disposable temporary copy, and counts a
mutation as caught only when the targeted test fails an assertion."""

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


# --- Classification: a mutation is caught only by an assertion failure in the targeted test. ---

TARGET = "tests/unit/test_x.py::test_guard"


def report(*cases: str) -> str:
    return f'<testsuites><testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>'


def case(name: str, child: str = "", classname: str = "tests.unit.test_x") -> str:
    return f'<testcase classname="{classname}" name="{name}">{child}</testcase>'


FAIL = '<failure message="assert 1 == 2">AssertionError</failure>'
ERROR = '<error message="failed on setup">RuntimeError</error>'
SKIP = '<skipped message="no database" />'


def test_targeted_assertion_failure_is_caught() -> None:
    bt = load()
    assert bt.classify(1, report(case("test_guard", FAIL)), TARGET).outcome is bt.Outcome.CAUGHT
    # A parametrized case of the target counts as the target.
    assert bt.classify(1, report(case("test_guard[a]", FAIL)), TARGET).outcome is bt.Outcome.CAUGHT
    # A whole-file target is matched by module.
    file_target = "tests/unit/test_x.py"
    assert bt.classify(1, report(case("test_other", FAIL)), file_target).outcome is bt.Outcome.CAUGHT


@pytest.mark.parametrize(
    ("returncode", "xml"),
    [
        pytest.param(1, report(case("test_guard", ERROR)), id="fixture-error"),
        pytest.param(1, report(case("test_guard", FAIL), case("test_other", ERROR)), id="failure-plus-error"),
        pytest.param(1, report(case("test_other", FAIL)), id="failure-only-elsewhere"),
        pytest.param(1, report(case("test_guard_extra", FAIL)), id="prefix-is-not-the-target"),
        pytest.param(1, report(case("test_guard", FAIL, classname="tests.unit.test_y")), id="other-module"),
        pytest.param(1, report(), id="exit-1-no-failures"),
        pytest.param(1, None, id="no-report"),
        pytest.param(1, "<testsuites", id="unreadable-report"),
        pytest.param(0, report(case("test_guard", SKIP)), id="exit-0-target-skipped"),
        pytest.param(0, report(case("test_other")), id="exit-0-target-not-run"),
        *(pytest.param(code, report(case("test_guard", FAIL)), id=f"exit-{code}") for code in (2, 3, 4, 5)),
    ],
)
def test_anything_else_is_a_harness_error_never_caught(returncode: int, xml: str | None) -> None:
    bt = load()
    assert bt.classify(returncode, xml, TARGET).outcome is bt.Outcome.HARNESS_ERROR


def test_a_passing_target_is_missed() -> None:
    bt = load()
    assert bt.classify(0, report(case("test_guard")), TARGET).outcome is bt.Outcome.MISSED


SAMPLE = """
import pytest


@pytest.fixture
def broken():
    raise RuntimeError("database unavailable")


def test_fails():
    assert 1 == 2


def test_passes():
    assert True


def test_needs_fixture(broken):
    assert True


def test_skips():
    pytest.skip("no database")
"""


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("tests/test_sample.py::test_fails", "CAUGHT"),
        ("tests/test_sample.py::test_needs_fixture", "HARNESS ERROR"),
        ("tests/test_sample.py::test_skips", "HARNESS ERROR"),
        ("tests/test_sample.py::test_missing", "HARNESS ERROR"),
        ("tests/test_sample.py::test_passes", "MISSED"),
    ],
)
def test_classifies_real_pytest_runs(tmp_path: Path, target: str, expected: str) -> None:
    """End to end against pytest's real JUnit output, including the fixture-error case."""
    bt = load()
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text(SAMPLE, encoding="utf-8")
    returncode, xml = bt.run_pytest(tmp_path, target)
    assert bt.classify(returncode, xml, target).outcome == expected
