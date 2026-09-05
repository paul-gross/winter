from __future__ import annotations

import sys
from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from tests.conftest import FakeFilesystem, FakeSubprocessRunner
from winter_cli.config.models import FileSizeLintConfig
from winter_cli.core.internal.local_subprocess_runner import LocalSubprocessRunner
from winter_cli.core.subprocess_runner import IStreamingProcess, ISubprocessRunner, SubprocessResult
from winter_cli.modules.lint.core_lint_service import (
    CORE_SOURCE,
    FILE_SIZE_CHECK,
    CoreLintService,
    FileSizeLintCheck,
    default_extractability_script_path,
)
from winter_cli.modules.lint.internal.gitignore_repository import GitIgnoreRepository
from winter_cli.modules.lint.models import LintScope, LintScopeKind, LintStatus

WORKSPACE_ROOT = Path("/ws")
SCRIPT_PATH = Path("/cli/tools/winter-lint/extractability.py")
SCOPE = LintScope(kind=LintScopeKind.all, label="all", paths=[WORKSPACE_ROOT])

# The fake runner keys responses by the joined command string.
_CMD_KEY = f"{sys.executable} {SCRIPT_PATH}"


def _build_service(
    *,
    files: dict[Path, str] | None = None,
    run_response: SubprocessResult | None = None,
    file_size_config: FileSizeLintConfig | None = None,
) -> tuple[CoreLintService, FakeSubprocessRunner]:
    fs = FakeFilesystem(
        files=files if files is not None else {SCRIPT_PATH: ""},
        directories={WORKSPACE_ROOT},
    )
    responses: dict[str, SubprocessResult] = {}
    if run_response is not None:
        responses[_CMD_KEY] = run_response
    runner = FakeSubprocessRunner(run_responses=responses)
    svc = CoreLintService(
        workspace_root=WORKSPACE_ROOT,
        fs=fs,
        subprocess_runner=runner,
        winter_cli_path="/usr/bin/winter",
        script_path=SCRIPT_PATH,
        file_size_config=file_size_config,
    )
    return svc, runner


def test_runs_bundled_script_with_lint_env() -> None:
    svc, runner = _build_service(run_response=SubprocessResult(0, "", ""))
    svc.run(SCOPE)
    assert runner.run_calls[-1][0] == [sys.executable, str(SCRIPT_PATH)]
    assert runner.run_calls[-1][1] == WORKSPACE_ROOT
    env = runner.run_envs[-1]
    assert env is not None
    assert env["WINTER_CLI"] == "/usr/bin/winter"
    assert env["WINTER_WORKSPACE_DIR"] == str(WORKSPACE_ROOT)
    assert env["WINTER_LINT_SCOPE"] == "all"
    assert env["WINTER_LINT_PATHS"] == str(WORKSPACE_ROOT)


def test_parses_findings_under_core_source() -> None:
    svc, _ = _build_service(
        run_response=SubprocessResult(
            0,
            '{"check": "extractability", "status": "fail", "message": "layering", "file": "context/x.md", "line": 3}\n',
            "",
        )
    )
    outcomes = svc.run(SCOPE)
    assert outcomes
    # extractability outcome is first
    outcome = outcomes[0]
    assert outcome.source == CORE_SOURCE
    finding = outcome.findings[0]
    assert finding.source == CORE_SOURCE
    assert finding.check == "extractability"
    assert finding.status == LintStatus.fail
    assert finding.file == "context/x.md"
    assert finding.line == 3


def test_clean_run_still_contributes_outcomes() -> None:
    svc, _ = _build_service(run_response=SubprocessResult(0, "", ""))
    outcomes = svc.run(SCOPE)
    # extractability, file-size, and required-services outcomes are all present.
    assert len(outcomes) == 3
    assert all(o.source == CORE_SOURCE for o in outcomes)


def test_non_zero_exit_becomes_synthetic_fail() -> None:
    svc, _ = _build_service(run_response=SubprocessResult(1, "", "graph fetch failed"))
    outcomes = svc.run(SCOPE)
    assert outcomes
    assert outcomes[0].findings[0].status == LintStatus.fail
    assert outcomes[0].findings[0].message == "graph fetch failed"


def test_missing_script_contributes_nothing() -> None:
    svc, runner = _build_service(files={})
    assert svc.run(SCOPE) == []
    assert runner.run_calls == []


def test_default_script_path_points_at_sibling_tools_dir() -> None:
    path = default_extractability_script_path()
    assert path.parts[-3:] == ("tools", "winter-lint", "extractability.py")


# ── FileSizeLintCheck tests ──────────────────────────────────────────────────


@pytest.fixture
def tmp_ws(tmp_path: Path) -> Path:
    """A scratch workspace directory on the real filesystem."""
    return tmp_path


@pytest.fixture
def real_runner() -> LocalSubprocessRunner:
    """A real subprocess runner, for the gitignore-discovery tests below whose
    correctness lives in parsing real `git`/`check-ignore` output."""
    return LocalSubprocessRunner()


class FakeGitIgnoreRepository:
    """IGitIgnoreRepository fake that never filters — the "no repo" result.

    The size-threshold tests below are about byte-counting and @import
    resolution, not gitignore discovery; standing up a real git repo for each
    would test the adapter twice over. This fake pins the "not a git repo"
    outcome those tests actually rely on without depending on `tmp_ws` staying
    outside whatever git repo the test machine's `$TMPDIR` happens to sit in.
    """

    def filter_ignored(self, scope_root: Path, candidates: list[Path]) -> list[Path]:
        return candidates


@pytest.fixture
def fake_gitignore_repo() -> FakeGitIgnoreRepository:
    return FakeGitIgnoreRepository()


def test_file_under_threshold_passes(tmp_ws: Path, fake_gitignore_repo: FakeGitIgnoreRepository) -> None:
    """A markdown file smaller than both thresholds produces no findings."""
    md = tmp_ws / "context" / "small.md"
    md.parent.mkdir(parents=True)
    md.write_text("# tiny\n")  # well under any threshold

    check = FileSizeLintCheck(
        tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=12000), fake_gitignore_repo
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)
    assert findings == []


def test_injected_file_over_tighter_threshold_fails(
    tmp_ws: Path, fake_gitignore_repo: FakeGitIgnoreRepository
) -> None:
    """A file in the @import graph that exceeds the injected threshold is flagged."""
    # Create CLAUDE.md that @imports context/index.md
    claude_md = tmp_ws / "CLAUDE.md"
    context_index = tmp_ws / "context" / "index.md"
    context_index.parent.mkdir(parents=True)

    # context/index.md is over the injected threshold (6000 bytes) but under reference (12000)
    oversized_content = "x" * 7000
    context_index.write_bytes(oversized_content.encode())

    claude_md.write_text("See @context/index.md for details.\n")

    check = FileSizeLintCheck(
        tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=12000), fake_gitignore_repo
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)

    injected_findings = [f for f in findings if f.check == FILE_SIZE_CHECK and "context/index.md" in (f.file or "")]
    assert injected_findings, f"Expected a finding for context/index.md; got {findings}"
    f = injected_findings[0]
    assert f.status == LintStatus.fail
    assert "7000" in f.message
    assert "injected" in f.message
    assert "6000" in f.message


def test_non_injected_file_between_thresholds_passes(
    tmp_ws: Path, fake_gitignore_repo: FakeGitIgnoreRepository
) -> None:
    """A non-injected reference doc between the two thresholds should not be flagged."""
    # No CLAUDE.md → nothing is injected.
    ref_doc = tmp_ws / "docs" / "reference.md"
    ref_doc.parent.mkdir(parents=True)

    # Between injected (6000) and reference (12000) thresholds.
    between_content = "y" * 8000
    ref_doc.write_bytes(between_content.encode())

    check = FileSizeLintCheck(
        tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=12000), fake_gitignore_repo
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)
    assert findings == [], f"Expected no findings for a reference doc under 12 000 bytes; got {findings}"


def test_padding_and_rules_do_not_count_toward_the_threshold(
    tmp_ws: Path, fake_gitignore_repo: FakeGitIgnoreRepository
) -> None:
    """A file whose raw size is over the threshold passes when the excess is formatting."""
    md = tmp_ws / "docs" / "wide-table.md"
    md.parent.mkdir(parents=True)
    # 200 content bytes, then 8 000 bytes of table padding and separator rule.
    md.write_text("x" * 200 + "\n| a" + " " * 4000 + "| b |\n| " + "-" * 4000 + " | --- |\n")
    assert md.stat().st_size > 6000

    check = FileSizeLintCheck(
        tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=6000), fake_gitignore_repo
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)
    assert findings == [], f"Expected padding and rules to be normalized away; got {findings}"


def test_finding_reports_effective_and_raw_sizes(tmp_ws: Path, fake_gitignore_repo: FakeGitIgnoreRepository) -> None:
    """The message names the effective size that failed and the raw size beside it."""
    md = tmp_ws / "docs" / "big.md"
    md.parent.mkdir(parents=True)
    md.write_text("y" * 7000 + " " * 500)
    raw = md.stat().st_size

    check = FileSizeLintCheck(
        tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=6000), fake_gitignore_repo
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)
    assert len(findings) == 1
    assert "7001 effective bytes" in findings[0].message
    assert f"({raw} raw)" in findings[0].message


def test_threshold_override_via_config(tmp_ws: Path, fake_gitignore_repo: FakeGitIgnoreRepository) -> None:
    """A very tight custom threshold flags a file that the default would pass."""
    md = tmp_ws / "small.md"
    md.write_bytes(b"hello world")  # 11 bytes

    # Override: flag anything over 5 bytes.
    check = FileSizeLintCheck(tmp_ws, FileSizeLintConfig(injected_bytes=5, reference_bytes=5), fake_gitignore_repo)
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)
    assert len(findings) == 1
    assert findings[0].check == FILE_SIZE_CHECK
    assert findings[0].status == LintStatus.fail


def test_agents_md_root_imports_are_injected(tmp_ws: Path, fake_gitignore_repo: FakeGitIgnoreRepository) -> None:
    """Files @imported from AGENTS.md are included in the injected set."""
    agents_md = tmp_ws / "AGENTS.md"
    context_index = tmp_ws / "context" / "index.md"
    context_index.parent.mkdir(parents=True)

    # context/index.md is over the injected threshold but under reference.
    oversized_content = "x" * 7000
    context_index.write_bytes(oversized_content.encode())

    agents_md.write_text("See @context/index.md for details.\n")

    check = FileSizeLintCheck(
        tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=12000), fake_gitignore_repo
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)

    injected_findings = [f for f in findings if f.check == FILE_SIZE_CHECK and "context/index.md" in (f.file or "")]
    assert injected_findings, f"Expected a finding for context/index.md via AGENTS.md root; got {findings}"
    assert "injected" in injected_findings[0].message


def test_agents_winter_md_root_imports_are_injected(
    tmp_ws: Path, fake_gitignore_repo: FakeGitIgnoreRepository
) -> None:
    """Files @imported from AGENTS.winter.md are included in the injected set."""
    agents_winter_md = tmp_ws / "AGENTS.winter.md"
    ext_index = tmp_ws / "ext" / "info.md"
    ext_index.parent.mkdir(parents=True)

    oversized_content = "y" * 7000
    ext_index.write_bytes(oversized_content.encode())

    agents_winter_md.write_text("Extension context: @ext/info.md\n")

    check = FileSizeLintCheck(
        tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=12000), fake_gitignore_repo
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)

    injected_findings = [f for f in findings if f.check == FILE_SIZE_CHECK and "ext/info.md" in (f.file or "")]
    assert injected_findings, f"Expected a finding for ext/info.md via AGENTS.winter.md root; got {findings}"
    assert "injected" in injected_findings[0].message


def test_dedup_when_claude_md_shims_to_agents_md(tmp_ws: Path, fake_gitignore_repo: FakeGitIgnoreRepository) -> None:
    """A file reachable via both CLAUDE.md → AGENTS.md and directly as AGENTS.md is counted once.

    Simulates the post-migration layout where CLAUDE.md is a shim containing
    ``@AGENTS.md``.  Any file imported by AGENTS.md that exceeds the injected
    threshold should produce exactly one finding, not two.
    """
    agents_md = tmp_ws / "AGENTS.md"
    claude_md = tmp_ws / "CLAUDE.md"
    shared_doc = tmp_ws / "context" / "shared.md"
    shared_doc.parent.mkdir(parents=True)

    oversized_content = "z" * 7000
    shared_doc.write_bytes(oversized_content.encode())

    # AGENTS.md imports the shared doc directly.
    agents_md.write_text("See @context/shared.md for details.\n")
    # CLAUDE.md is a shim that @imports AGENTS.md, making shared.md reachable via two paths.
    claude_md.write_text("@AGENTS.md\n")

    check = FileSizeLintCheck(
        tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=12000), fake_gitignore_repo
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)

    shared_findings = [f for f in findings if f.check == FILE_SIZE_CHECK and "context/shared.md" in (f.file or "")]
    assert len(shared_findings) == 1, (
        f"Expected exactly one finding for context/shared.md (dedup); got {len(shared_findings)}: {shared_findings}"
    )


# ── gitignore-aware discovery ────────────────────────────────────────────────
#
# These tests back the import-site parsing of real `git check-ignore` output
# with real I/O — the property `standards/testing.md` reserves real-I/O tests
# for (an adapter whose correctness lives in parsing an external tool's
# output), unlike the size-threshold tests above.


def _init_git_repo(root: Path, runner: LocalSubprocessRunner) -> None:
    result = runner.run(["git", "init"], cwd=root)
    assert result.returncode == 0, result.stderr


def test_gitignored_vendored_tree_produces_no_finding(tmp_ws: Path, real_runner: LocalSubprocessRunner) -> None:
    """A file under a repo's gitignored vendored tree (e.g. Terraform's
    `.terraform/`) is skipped entirely — no `[lint.ignore]` rule needed for
    content the repo already told git to disregard."""
    _init_git_repo(tmp_ws, real_runner)
    (tmp_ws / ".gitignore").write_text("terraform/**/.terraform/**\n")

    vendored = tmp_ws / "terraform" / "modules" / ".terraform" / "modules" / "foo" / "README.md"
    vendored.parent.mkdir(parents=True)
    vendored.write_bytes(b"x" * 7000)

    check = FileSizeLintCheck(
        tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=6000), GitIgnoreRepository(real_runner)
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)

    assert findings == [], f"Expected the gitignored vendored file to produce no finding; got {findings}"


def test_untracked_not_ignored_file_still_checked(tmp_ws: Path, real_runner: LocalSubprocessRunner) -> None:
    """A file that is untracked but not gitignored stays in scope — only
    content git was told to disregard is dropped."""
    _init_git_repo(tmp_ws, real_runner)
    (tmp_ws / ".gitignore").write_text("terraform/**/.terraform/**\n")

    fresh_doc = tmp_ws / "docs" / "fresh.md"
    fresh_doc.parent.mkdir(parents=True)
    fresh_doc.write_bytes(b"y" * 7000)  # never `git add`-ed, and not ignored

    check = FileSizeLintCheck(
        tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=6000), GitIgnoreRepository(real_runner)
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)

    assert len(findings) == 1
    assert "docs/fresh.md" in (findings[0].file or "")


def test_tracked_file_matching_ignore_pattern_still_checked(tmp_ws: Path, real_runner: LocalSubprocessRunner) -> None:
    """A file `git add -f`-ed despite matching a `.gitignore` pattern stays in scope.

    `check-ignore` (without `--no-index`) never reports a *tracked* path —
    "By default, tracked files are not shown at all since they are not
    subject to exclude rules." That's the property this whole feature leans
    on to stay safe: a future change that adds `--no-index`, or swaps to a
    different git plumbing command, must not start dropping tracked agent-facing
    markdown just because it happens to match an ignore pattern.
    """
    _init_git_repo(tmp_ws, real_runner)
    (tmp_ws / ".gitignore").write_text("docs/**\n")

    tracked = tmp_ws / "docs" / "tracked.md"
    tracked.parent.mkdir(parents=True)
    tracked.write_bytes(b"y" * 7000)
    add_result = real_runner.run(["git", "add", "-f", "docs/tracked.md"], cwd=tmp_ws)
    assert add_result.returncode == 0, add_result.stderr

    check = FileSizeLintCheck(
        tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=6000), GitIgnoreRepository(real_runner)
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)

    assert len(findings) == 1
    assert "docs/tracked.md" in (findings[0].file or "")


def test_non_git_scope_path_falls_back_to_prune_dirs(tmp_ws: Path, real_runner: LocalSubprocessRunner) -> None:
    """A scope path outside any git repository still walks — falling back to
    `_PRUNE_DIRS`-only filtering rather than erroring."""
    # Precondition pinning the branch this test names: `tmp_ws` really isn't
    # inside a git repository, so `rev-parse --show-toplevel` fails and
    # `GitIgnoreRepository` takes the non-git fallback path, not some other
    # route that happens to leave `docs/big.md` unfiltered.
    toplevel_probe = real_runner.run(["git", "rev-parse", "--show-toplevel"], cwd=tmp_ws)
    assert toplevel_probe.returncode != 0, (
        f"expected {tmp_ws} to be outside any git repository; got toplevel {toplevel_probe.stdout!r}"
    )

    oversized = tmp_ws / "docs" / "big.md"
    oversized.parent.mkdir(parents=True)
    oversized.write_bytes(b"z" * 7000)

    check = FileSizeLintCheck(
        tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=6000), GitIgnoreRepository(real_runner)
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)

    assert len(findings) == 1
    assert "docs/big.md" in (findings[0].file or "")


def test_ignore_rules_resolved_per_repo_not_per_workspace(tmp_ws: Path, real_runner: LocalSubprocessRunner) -> None:
    """A scope root's own repo is consulted, not whichever repo encloses `workspace_root`.

    `workspace_root` and the scope root are deliberately different paths here
    — `workspace_root` is a plain (non-git) directory, and the scope is a
    repo one level below it (`workspace_root/repoA`) carrying its own
    `.gitignore`. Replacing the adapter's `cwd=scope_root` with
    `cwd=workspace_root` would either miss the ignore rule entirely (no repo
    at `workspace_root`) or, worse, pick up an unrelated enclosing repo's
    rules — this test fails either way.
    """
    repo_a = tmp_ws / "repoA"
    repo_a.mkdir()
    _init_git_repo(repo_a, real_runner)
    (repo_a / ".gitignore").write_text("vendor/**\n")

    vendored = repo_a / "vendor" / "third_party.md"
    vendored.parent.mkdir(parents=True)
    vendored.write_bytes(b"x" * 7000)

    check = FileSizeLintCheck(
        tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=6000), GitIgnoreRepository(real_runner)
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[repo_a])
    findings = check.check(scope)

    assert findings == [], f"Expected repoA's own .gitignore to drop the vendored file; got {findings}"


class _CountingSubprocessRunner:
    """Wraps a real `ISubprocessRunner`, counting `run` invocations.

    Lets a test assert ignore resolution is batched — one `git check-ignore`
    call per repo, not one per candidate file — without depending on the
    exact argv order `os.walk` happens to produce.
    """

    def __init__(self, inner: ISubprocessRunner) -> None:
        self._inner = inner
        self.run_calls: list[list[str]] = []

    def run(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> SubprocessResult:
        self.run_calls.append(cmd)
        return self._inner.run(cmd, cwd=cwd, env=env)

    def call(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> int:
        return self._inner.call(cmd, cwd=cwd, env=env)

    def popen(
        self,
        cmd: list[str] | str,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        shell: bool = False,
        merge_stderr: bool = True,
    ) -> AbstractContextManager[IStreamingProcess]:
        return self._inner.popen(cmd, cwd=cwd, env=env, shell=shell, merge_stderr=merge_stderr)


def test_ignore_resolution_batched_per_repo_not_per_file(tmp_ws: Path, real_runner: LocalSubprocessRunner) -> None:
    """Many candidate files under one repo still cost a single `check-ignore` call."""
    _init_git_repo(tmp_ws, real_runner)
    (tmp_ws / ".gitignore").write_text("ignored/**\n")

    for i in range(5):
        f = tmp_ws / "docs" / f"doc{i}.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# doc {i}\n")
    ignored_dir = tmp_ws / "ignored"
    ignored_dir.mkdir()
    (ignored_dir / "vendor.md").write_text("# vendored\n")

    counting = _CountingSubprocessRunner(real_runner)
    check = FileSizeLintCheck(
        tmp_ws,
        FileSizeLintConfig(injected_bytes=100_000, reference_bytes=100_000),
        GitIgnoreRepository(counting),
    )
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    check.check(scope)

    check_ignore_calls = [c for c in counting.run_calls if "check-ignore" in c]
    assert len(check_ignore_calls) == 1, f"Expected exactly one check-ignore call; got {check_ignore_calls}"
