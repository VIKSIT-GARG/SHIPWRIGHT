"""Tests for shipwright.runner: CommandRunner and CommandExecution."""

import sys
import tempfile
from pathlib import Path

import pytest

from shipwright.runner import CommandExecution, CommandRunner, detect_test_command


def test_command_execution_fields_and_slots() -> None:
    exec_result = CommandExecution(
        cmd=["echo", "hello"],
        cwd="/tmp",
        exit_code=0,
        stdout="hello\n",
        stderr="",
        duration=0.042,
    )

    assert exec_result.cmd == ["echo", "hello"]
    assert exec_result.cwd == "/tmp"
    assert exec_result.exit_code == 0
    assert exec_result.stdout == "hello\n"
    assert exec_result.stderr == ""
    assert exec_result.duration == 0.042
    assert exec_result.success is True
    assert exec_result.failed is False
    assert exec_result.combined_output == "hello\n"
    assert hasattr(exec_result, "__slots__")


def test_command_execution_failed_property() -> None:
    exec_result = CommandExecution(
        cmd=["false"],
        cwd="/tmp",
        exit_code=1,
        stdout="",
        stderr="error occurred",
        duration=0.1,
    )

    assert exec_result.exit_code == 1
    assert exec_result.success is False
    assert exec_result.failed is True
    assert exec_result.combined_output == "error occurred"


def test_command_runner_simple_success(tmp_path: Path) -> None:
    runner = CommandRunner()
    result = runner.run(
        [sys.executable, "-c", "print('hello from runner')"],
        cwd=tmp_path,
    )

    assert result.exit_code == 0
    assert result.success is True
    assert result.failed is False
    assert result.stdout.strip() == "hello from runner"
    assert result.stderr == ""
    assert result.cwd == str(tmp_path)
    assert result.duration > 0


def test_command_runner_failure_exit_code(tmp_path: Path) -> None:
    runner = CommandRunner()
    result = runner.run(
        [sys.executable, "-c", "import sys; sys.stderr.write('fatal\\n'); sys.exit(42)"],
        cwd=str(tmp_path),
    )

    assert result.exit_code == 42
    assert result.success is False
    assert result.failed is True
    assert "fatal" in result.stderr


def test_command_runner_environment_passing(tmp_path: Path) -> None:
    runner = CommandRunner()
    result = runner.run(
        [sys.executable, "-c", "import os; print(os.environ.get('CUSTOM_RUNNER_VAR', ''))"],
        cwd=tmp_path,
        env={"CUSTOM_RUNNER_VAR": "shipwright_active"},
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "shipwright_active"


def test_command_runner_truncates_large_output(tmp_path: Path) -> None:
    runner = CommandRunner()
    large_size = 1000
    limit = 250

    result = runner.run(
        [sys.executable, "-c", f"print('x' * {large_size})"],
        cwd=tmp_path,
        max_output_chars=limit,
    )

    assert len(result.stdout) == limit
    assert result.stdout == "x" * limit


def test_command_runner_timeout_expired(tmp_path: Path) -> None:
    runner = CommandRunner()
    result = runner.run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout=0.2,
    )

    assert result.exit_code != 0
    assert result.failed is True
    assert "timed out" in result.stderr.lower()
    assert result.duration >= 0.15


def test_detect_test_command_pyproject_toml(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "pyproject.toml").touch()

    cmd = runner.detect_test_command(tmp_path)
    assert cmd == ["python", "-m", "pytest", "-q"]


def test_detect_test_command_setup_py(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "setup.py").touch()

    cmd = runner.detect_test_command(tmp_path)
    assert cmd == ["python", "-m", "pytest", "-q"]


def test_detect_test_command_setup_cfg(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "setup.cfg").touch()

    cmd = runner.detect_test_command(tmp_path)
    assert cmd == ["python", "-m", "pytest", "-q"]


def test_detect_test_command_tests_directory(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "tests").mkdir()

    cmd = runner.detect_test_command(tmp_path)
    assert cmd == ["python", "-m", "pytest", "-q"]


def test_detect_test_command_unittest_files(tmp_path: Path) -> None:
    runner = CommandRunner()
    test_file = tmp_path / "test_math.py"
    test_file.write_text("import unittest\nclass MathTest(unittest.TestCase): pass\n")

    cmd = runner.detect_test_command(tmp_path)
    assert cmd == ["python", "-m", "unittest"]


def test_detect_test_command_empty_test_file(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "test_example.py").touch()

    cmd = runner.detect_test_command(tmp_path)
    assert cmd == ["python", "-m", "unittest"]


def test_detect_test_command_default_pytest(tmp_path: Path) -> None:
    runner = CommandRunner()
    # No test files, no pytest configs
    (tmp_path / "app.py").write_text("print('hello world')")

    cmd = runner.detect_test_command(tmp_path)
    assert cmd == ["python", "-m", "pytest", "-q"]


def test_detect_test_command_nonexistent_path(tmp_path: Path) -> None:
    runner = CommandRunner()
    nonexistent = tmp_path / "does_not_exist"

    cmd = runner.detect_test_command(nonexistent)
    assert cmd == ["python", "-m", "pytest", "-q"]


def test_detect_test_command_standalone_function(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").touch()
    assert detect_test_command(tmp_path) == ["cargo", "test"]


def test_detect_test_command_js_npm(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    assert runner.detect_test_command(tmp_path) == ["npm", "test"]


def test_detect_test_command_js_pnpm(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").touch()
    assert runner.detect_test_command(tmp_path) == ["pnpm", "test"]


def test_detect_test_command_js_yarn(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    (tmp_path / "yarn.lock").touch()
    assert runner.detect_test_command(tmp_path) == ["yarn", "test"]


def test_detect_test_command_js_bun(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "package.json").write_text('{"scripts": {"test": "bun test"}}', encoding="utf-8")
    (tmp_path / "bun.lockb").touch()
    assert runner.detect_test_command(tmp_path) == ["bun", "test"]


def test_detect_test_command_js_bun_lock(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "package.json").write_text('{"scripts": {"test": "bun test"}}', encoding="utf-8")
    (tmp_path / "bun.lock").touch()
    assert runner.detect_test_command(tmp_path) == ["bun", "test"]


def test_detect_test_command_js_no_test_script(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "package.json").write_text('{"name": "myapp", "scripts": {"build": "tsc"}}', encoding="utf-8")
    assert runner.detect_test_command(tmp_path) == ["python", "-m", "pytest", "-q"]


def test_detect_test_command_rust_cargo(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "Cargo.toml").touch()
    assert runner.detect_test_command(tmp_path) == ["cargo", "test"]


def test_detect_test_command_go_mod(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "go.mod").touch()
    assert runner.detect_test_command(tmp_path) == ["go", "test", "./..."]


def test_detect_test_command_ruby_gemfile_rake(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'rails'", encoding="utf-8")
    assert runner.detect_test_command(tmp_path) == ["bundle", "exec", "rake", "test"]


def test_detect_test_command_ruby_gemfile_rspec(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'rspec'", encoding="utf-8")
    assert runner.detect_test_command(tmp_path) == ["bundle", "exec", "rspec"]


def test_detect_test_command_ruby_spec_dir(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "Gemfile").touch()
    (tmp_path / "spec").mkdir()
    assert runner.detect_test_command(tmp_path) == ["bundle", "exec", "rspec"]


def test_detect_test_command_ruby_dot_rspec(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "Gemfile").touch()
    (tmp_path / ".rspec").touch()
    assert runner.detect_test_command(tmp_path) == ["bundle", "exec", "rspec"]


def test_detect_test_command_ruby_rakefile(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "Rakefile").touch()
    assert runner.detect_test_command(tmp_path) == ["bundle", "exec", "rake", "test"]


def test_detect_test_command_java_gradlew(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "gradlew").touch()
    assert runner.detect_test_command(tmp_path) == ["./gradlew", "test"]


def test_detect_test_command_java_mvn(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "pom.xml").touch()
    assert runner.detect_test_command(tmp_path) == ["mvn", "test"]


def test_detect_test_command_c_makefile_test(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "Makefile").write_text("all:\n\tgcc main.c\ntest:\n\t./test_runner\n", encoding="utf-8")
    assert runner.detect_test_command(tmp_path) == ["make", "test"]


def test_detect_test_command_c_makefile_check(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "Makefile").write_text("all:\n\tgcc main.c\ncheck:\n\t./check_runner\n", encoding="utf-8")
    assert runner.detect_test_command(tmp_path) == ["make", "check"]


def test_detect_test_command_c_makefile_default(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "Makefile").touch()
    assert runner.detect_test_command(tmp_path) == ["make", "test"]


def test_detect_test_command_c_cmake(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "CMakeLists.txt").touch()
    assert runner.detect_test_command(tmp_path) == ["ctest"]


def test_detect_test_command_precedence_python_over_js_and_makefile(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    (tmp_path / "Makefile").touch()
    assert runner.detect_test_command(tmp_path) == ["python", "-m", "pytest", "-q"]

