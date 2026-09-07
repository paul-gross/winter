from __future__ import annotations

import dataclasses
import enum
import logging
from pathlib import Path

import git

from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import (
    DiffMode,
    FeatureWorktree,
    ProjectRepository,
    RepoCommit,
    RepoDiffResult,
    RepoHistory,
    RepoStatus,
    RepoStatusAndHistory,
    StandaloneRepository,
    StandaloneRepoStatus,
    Workspace,
)
from winter_cli.modules.workspace.repo_repository import IReadRepoRepository

logger = logging.getLogger(__name__)

# `git log --graph` format used to build the commit graph and recent-commits list
# in one call. A leading \x00 sentinel separates git's graph glyphs from the
# commit text, so the glyph run is isolated without depending on the abbreviated
# hash width (core.abbrev). After the sentinel comes the `--oneline --decorate`
# equivalent `%h%d %s` — rejoined to its glyphs it renders the graph line
# byte-identically — then a \x1f-delimited trailer carrying the full hash and
# clean subject for recent_commits.
_GRAPH_FORMAT = "--format=%x00%h%d %s%x1f%H%x1f%s"


@dataclasses.dataclass(frozen=True)
class _PorcelainStatus:
    """Parsed fields of a `git status --porcelain=v2 --branch` capture."""

    branch: str | None
    tracking_branch: str | None
    tracking_ahead: int
    tracking_behind: int
    tracking_ref_present: bool
    staged_files: list[str]
    unstaged_files: list[str]
    untracked_files: list[str]

    @property
    def dirty_files(self) -> list[str]:
        # Dedup paths that are both staged and unstaged (partially-staged files),
        # preserving order, then append untracked — matching the old GitPython build.
        files = list(dict.fromkeys(self.staged_files + self.unstaged_files))
        files += self.untracked_files
        return files


def _parse_status_porcelain_v2(out: str) -> _PorcelainStatus:
    """Parse `git status --porcelain=v2 --branch --untracked-files=all -z` output.

    Replaces the active-branch, tracking-branch, both tracking `rev-list` probes,
    and the three index/worktree/untracked diff probes with one parse. The `-z`
    framing leaves paths unquoted, so filenames with spaces or quotes survive
    intact; `--untracked-files=all` matches GitPython's recursive `untracked_files`.
    """
    branch: str | None = None
    tracking_branch: str | None = None
    tracking_ahead = 0
    tracking_behind = 0
    tracking_ref_present = False
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []

    # `-z` emits NUL-terminated records (the trailing NUL leaves a final empty
    # token we drop). Rename/copy records (type 2) carry the original path in the
    # *next* record, so iterate with an explicit index to consume it.
    records = [rec for rec in out.split("\x00") if rec]
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if rec.startswith("# "):
            header = rec[2:]
            if header.startswith("branch.head "):
                head = header[len("branch.head ") :]
                # `(detached)` is git's no-active-branch sentinel — branch is None
                # whenever HEAD is not on a named local branch.
                branch = None if head == "(detached)" else head
            elif header.startswith("branch.upstream "):
                tracking_branch = header[len("branch.upstream ") :]
            elif header.startswith("branch.ab "):
                # branch.ab is emitted only when the upstream ref resolves, so its
                # presence *is* the tracking-ref-present signal (an unfetched
                # upstream still prints branch.upstream but omits branch.ab).
                # Format: "+<ahead> -<behind>".
                tracking_ref_present = True
                ahead_tok, behind_tok = header[len("branch.ab ") :].split()
                tracking_ahead = int(ahead_tok)
                tracking_behind = -int(behind_tok)
            continue

        kind = rec[0]
        if kind == "?":
            untracked.append(rec[2:])
            continue
        if kind == "u":
            # Unmerged paths diverge in both index and worktree; GitPython listed
            # them in both index.diff("HEAD") and index.diff(None), so mirror that.
            staged.append(rec.split(" ", 10)[10])
            unstaged.append(rec.split(" ", 10)[10])
            continue
        if kind not in ("1", "2"):
            continue
        # The XY field is index/worktree status: X != "." means staged, Y != "."
        # means unstaged. Ordinary (type 1) path is field 8; rename/copy (type 2)
        # path is field 9, with the original path in the record we skip below.
        xy = rec.split(" ", 2)[1]
        if kind == "1":
            path = rec.split(" ", 8)[8]
        else:
            path = rec.split(" ", 9)[9]
            i += 1
        if xy[0] != ".":
            staged.append(path)
        if xy[1] != ".":
            unstaged.append(path)

    return _PorcelainStatus(
        branch=branch,
        tracking_branch=tracking_branch,
        tracking_ahead=tracking_ahead,
        tracking_behind=tracking_behind,
        tracking_ref_present=tracking_ref_present,
        staged_files=staged,
        unstaged_files=unstaged,
        untracked_files=untracked,
    )


def _parse_main_ahead_behind(out: str) -> tuple[int, int]:
    """Parse `git rev-list --left-right --count origin/<main>...HEAD` output.

    Prints "<left>\t<right>": left counts commits only on origin/<main> (behind),
    right counts commits only on HEAD (ahead). Returns `(ahead, behind)`.
    """
    behind_tok, ahead_tok = out.split()
    return int(ahead_tok), int(behind_tok)


def _parse_graph_log(out: str) -> tuple[list[str], list[RepoCommit]]:
    """Split a `git log --graph` capture (built with `_GRAPH_FORMAT`) into the
    rendered graph lines and the structured commit list.

    Each commit line is "<glyphs>\x00<rendered>\x1f<full-hash>\x1f<subject>"; pure
    connector lines (merge glyphs) carry no commit text, so no \x00. Boundary
    commits — the merge-base surfaced by `--boundary` — render with an `o` node
    glyph and are excluded from the commit list to match the old `iter_commits`
    walk (which never yields them).
    """
    graph_lines: list[str] = []
    commits: list[RepoCommit] = []
    for line in out.splitlines():
        # The \x00 sentinel divides git's graph glyphs from the commit text; a line
        # without it is a pure connector (merge glyphs), kept in the graph as-is.
        glyphs, sentinel, rest = line.partition("\x00")
        if not sentinel:
            graph_lines.append(line)
            continue
        rendered_tail, _, trailer = rest.partition("\x1f")
        # Rejoining the glyphs with the rendered text (dropping only the sentinel)
        # reproduces the `--oneline --decorate` graph line byte-for-byte.
        graph_lines.append(glyphs + rendered_tail)
        full_hash, _, subject = trailer.partition("\x1f")
        # `o` is git's boundary-node glyph and never appears in a connector run, so
        # its presence marks a boundary commit. Reading it from the isolated glyph
        # run (not the rendered line) keeps this independent of core.abbrev — the
        # hash lives after the sentinel, never in `glyphs`.
        if "o" in glyphs:
            continue
        commits.append(RepoCommit(short_hash=full_hash[:7], message=subject))
    return graph_lines, commits


class _Piece(enum.Flag):
    """Which piece(s) an open-repo visit gathers — an explicit parameter of the visit.

    `STATUS` runs the porcelain-v2 status call plus the main-branch `rev-list`;
    `HISTORY` runs the expensive `git log --graph` walk; `TIP_SUBJECT` reads
    `ahead` from the already-gathered `STATUS` piece and, only when HEAD is
    ahead of `origin/<main>`, runs a single minimal `git log -1 --format=%s`.
    A caller composes exactly the pieces its surface renders, so e.g. the
    dashboard grid never pays for `HISTORY` and `ws status` never pays for a
    full history walk just to read the tip subject.
    """

    STATUS = enum.auto()
    HISTORY = enum.auto()
    TIP_SUBJECT = enum.auto()


@dataclasses.dataclass
class _Visit:
    """Result of one open-repo visit — only the requested pieces are populated."""

    status: _PorcelainStatus | None = None
    ahead: int = 0
    behind: int = 0
    commit_graph: list[str] = dataclasses.field(default_factory=list)
    recent_commits: list[RepoCommit] = dataclasses.field(default_factory=list)
    tip_subject: str | None = None


class ReadRepoRepository:
    """Read-only GitPython implementation. All GitPython usage is confined here."""

    def __init__(self, error_factory: RepoErrorFactory) -> None:
        self._error_factory = error_factory

    def get_worktree_status(self, worktree: FeatureWorktree) -> RepoStatus:
        return self._build_status(
            worktree.path, worktree.repository.name, worktree.repository.main_branch, pieces=_Piece.STATUS
        )

    def get_worktree_status_for_snapshot(self, worktree: FeatureWorktree) -> RepoStatus:
        # `ws status`'s `last_commit_subject` consumer: one visit, status plus a
        # minimal tip-subject probe (gated on `ahead`, so it's null at parity
        # with origin/<main> — same as the pre-refactor recent_commits[0] read)
        # — never the full history walk.
        return self._build_status(
            worktree.path,
            worktree.repository.name,
            worktree.repository.main_branch,
            pieces=_Piece.STATUS | _Piece.TIP_SUBJECT,
        )

    def get_worktree_status_and_history(self, worktree: FeatureWorktree) -> RepoStatusAndHistory:
        return self._build_status_and_history(worktree.path, worktree.repository.name, worktree.repository.main_branch)

    def get_standalone_detail(self, repo: StandaloneRepository) -> RepoStatusAndHistory:
        # The standalone detail screen reuses the worktree detail's composite
        # shape (branch / tracking / dirty files / recent commits). Unlike a
        # feature worktree, a standalone has no feature branch ahead of main, so
        # `recent_from_head` lists the tip commits on HEAD itself — otherwise a
        # standalone with no configured `main_branch` would show an empty
        # history.
        return self._build_status_and_history(repo.path, repo.name, repo.main_branch, recent_from_head=True)

    def get_standalone_status(self, repo: StandaloneRepository) -> StandaloneRepoStatus:
        # Missing-on-disk / not-a-repo aren't errors — the dashboard renders
        # the row as "not present" and the user knows to run init.
        if not repo.path.exists():
            return StandaloneRepoStatus(repository=repo)

        # InvalidGitRepositoryError / NoSuchPathError are raised by the
        # `git.Repo(...)` constructor before `__enter__`, not by methods on an
        # open Repo, so this outer `except` still only catches construction
        # failures — the wider `try` body is structural, not a widened catch.
        try:
            with git.Repo(str(repo.path)) as r:
                # TypeError on detached HEAD, ValueError on unborn HEAD — both are
                # legitimate "no active branch" states from GitPython's API.
                try:
                    branch = r.active_branch.name
                except (TypeError, ValueError):
                    branch = None

                try:
                    # git.BadName is raised by r.index.diff("HEAD") when the repo has no commits yet
                    # (unborn HEAD). Treat that as "no staged changes."
                    try:
                        staged_paths = {item.a_path for item in r.index.diff("HEAD") if item.a_path is not None}
                    except git.BadName:
                        staged_paths = set()
                    unstaged_paths = {item.a_path for item in r.index.diff(None) if item.a_path is not None}
                    dirty_count = len(staged_paths | unstaged_paths) + len(r.untracked_files)
                except git.GitCommandError as exc:
                    raise self._error_factory.from_git(
                        exc,
                        message=f"dirty-count probe failed for {repo.name}",
                        cwd=repo.path,
                    ) from exc

                ahead = 0
                behind = 0
                try:
                    tb = r.active_branch.tracking_branch()
                except (TypeError, ValueError):
                    tb = None
                if tb is not None:
                    try:
                        ahead = int(r.git.rev_list("--count", f"{tb.name}..HEAD"))
                        behind = int(r.git.rev_list("--count", f"HEAD..{tb.name}"))
                    except git.GitCommandError as exc:
                        raise self._error_factory.from_git(
                            exc,
                            message=f"tracking ahead/behind probe failed for {repo.name}",
                            cwd=repo.path,
                        ) from exc

                # ValueError / IndexError protect against unborn HEAD and empty messages;
                # both are legitimate "no commit yet" states, not git errors.
                latest_commit: str | None = None
                try:
                    head_commit = r.head.commit
                    # GitPython types `Commit.message` as `bytes | str`; decode the bytes case.
                    message = head_commit.message
                    if isinstance(message, bytes):
                        message = message.decode("utf-8", errors="replace")
                    latest_commit = message.strip().splitlines()[0]
                except (ValueError, IndexError):
                    pass

                return StandaloneRepoStatus(
                    repository=repo,
                    branch=branch,
                    ahead=ahead,
                    behind=behind,
                    dirty_count=dirty_count,
                    tracking_ahead=0,
                    latest_commit=latest_commit,
                )
        except (git.InvalidGitRepositoryError, git.NoSuchPathError):
            return StandaloneRepoStatus(repository=repo)

    def get_project_status(self, repo: ProjectRepository) -> RepoStatus:
        return self._build_status(repo.main_path, repo.name, repo.main_branch, pieces=_Piece.STATUS)

    def get_diff(self, worktree: FeatureWorktree, mode: DiffMode) -> RepoDiffResult:
        name = worktree.repository.name
        with git.Repo(str(worktree.path)) as r:
            src_prefix = f"--src-prefix=a/{name}/"
            dst_prefix = f"--dst-prefix=b/{name}/"

            try:
                if mode == DiffMode.uncommitted:
                    diff_text = r.git.diff(src_prefix, dst_prefix)
                    numstat = r.git.diff("--numstat")
                elif mode == DiffMode.staged:
                    diff_text = r.git.diff("--staged", src_prefix, dst_prefix)
                    numstat = r.git.diff("--staged", "--numstat")
                else:
                    main_branch = worktree.repository.main_branch
                    ref = f"origin/{main_branch}...HEAD"
                    diff_text = r.git.diff(ref, src_prefix, dst_prefix)
                    numstat = r.git.diff(ref, "--numstat")
            except git.GitCommandError as exc:
                raise self._error_factory.from_git(
                    exc,
                    message=f"diff failed for {name}",
                    cwd=worktree.path,
                ) from exc

            files_changed = 0
            insertions = 0
            deletions = 0
            for line in numstat.splitlines():
                if not line:
                    continue
                parts = line.split("\t", 2)
                added = int(parts[0]) if parts[0] != "-" else 0
                removed = int(parts[1]) if parts[1] != "-" else 0
                insertions += added
                deletions += removed
                files_changed += 1

            ahead = 0
            if mode == DiffMode.branch:
                main_branch = worktree.repository.main_branch
                main_ref = f"origin/{main_branch}"
                try:
                    ahead = int(r.git.rev_list("--count", f"{main_ref}..HEAD"))
                except git.GitCommandError as exc:
                    # Same missing-ref tolerance as _read_main_ahead_behind: a
                    # brand-new clone with no `origin/<main>` yet returns 0 commits ahead.
                    try:
                        r.git.rev_parse("--verify", "--quiet", main_ref)
                    except git.GitCommandError:
                        pass
                    else:
                        raise self._error_factory.from_git(
                            exc,
                            message=f"branch ahead probe failed for {name}",
                            cwd=worktree.path,
                        ) from exc

            return RepoDiffResult(
                repo_name=name,
                diff_text=diff_text,
                ahead=ahead,
                files_changed=files_changed,
                insertions=insertions,
                deletions=deletions,
            )

    def get_workspace(
        self,
        root_path: Path,
        service_prefix: str,
        main_branch: str,
        base_port: int = 4000,
        ports_per_env: int = 20,
    ) -> Workspace:
        return Workspace(
            root_path=root_path,
            service_prefix=service_prefix,
            main_branch=main_branch,
            base_port=base_port,
            ports_per_env=ports_per_env,
        )

    def _build_status(
        self,
        repo_path: Path,
        name: str,
        main_branch: str | None,
        *,
        pieces: _Piece,
    ) -> RepoStatus:
        """Build the lean `RepoStatus` piece from one open-repo visit.

        `pieces` never includes `HISTORY` here — callers that need history go
        through `_build_status_and_history` instead, so the grid / `ws status`
        / project-status paths that call this never pay for `git log --graph`.
        """
        visit = self._visit(repo_path, name, main_branch, pieces=pieces)
        return self._status_from_visit(name, repo_path, main_branch, visit)

    def _build_status_and_history(
        self,
        repo_path: Path,
        name: str,
        main_branch: str | None,
        recent_from_head: bool = False,
    ) -> RepoStatusAndHistory:
        visit = self._visit(
            repo_path,
            name,
            main_branch,
            pieces=_Piece.STATUS | _Piece.HISTORY,
            recent_from_head=recent_from_head,
        )
        status = self._status_from_visit(name, repo_path, main_branch, visit)
        history = RepoHistory(commit_graph=visit.commit_graph, recent_commits=visit.recent_commits)
        return RepoStatusAndHistory(status=status, history=history)

    @staticmethod
    def _status_from_visit(name: str, repo_path: Path, main_branch: str | None, visit: _Visit) -> RepoStatus:
        if visit.status is None:
            return RepoStatus(name=name, path=str(repo_path), main_branch=main_branch)
        s = visit.status
        return RepoStatus(
            name=name,
            path=str(repo_path),
            main_branch=main_branch,
            branch=s.branch,
            ahead=visit.ahead,
            behind=visit.behind,
            dirty_files=s.dirty_files,
            staged_count=len(s.staged_files),
            unstaged_count=len(s.unstaged_files),
            untracked_count=len(s.untracked_files),
            tracking_branch=s.tracking_branch,
            tracking_ahead=s.tracking_ahead,
            tracking_behind=s.tracking_behind,
            tracking_ref_present=s.tracking_ref_present,
            last_commit_subject=visit.tip_subject,
        )

    def _visit(
        self,
        repo_path: Path,
        name: str,
        main_branch: str | None,
        *,
        pieces: _Piece,
        recent_from_head: bool = False,
    ) -> _Visit:
        """Open the repo once and gather exactly the requested `pieces`.

        The single `git.Repo` open per repo per gathering exercise: every
        piece-probe below (`_read_status`, `_read_main_ahead_behind`,
        `_read_history`, `_read_tip_subject`) runs against the same open `r`.
        Missing-on-disk / not-a-repo aren't errors — they're legitimate "this
        worktree hasn't been provisioned yet" states the caller renders as an
        empty row.
        """
        if not repo_path.exists():
            return _Visit()

        # InvalidGitRepositoryError / NoSuchPathError are raised by the
        # `git.Repo(...)` constructor before `__enter__`, not by methods on an
        # open Repo, so this outer `except` still only catches construction
        # failures — the wider `try` body is structural, not a widened catch.
        try:
            with git.Repo(str(repo_path)) as r:
                status: _PorcelainStatus | None = None
                ahead = behind = 0
                commit_graph: list[str] = []
                recent_commits: list[RepoCommit] = []
                tip_subject: str | None = None

                if _Piece.STATUS in pieces:
                    status = self._read_status(r, name, repo_path)
                    ahead, behind = self._read_main_ahead_behind(r, name, repo_path, main_branch)
                if _Piece.HISTORY in pieces:
                    commit_graph, recent_commits = self._read_history(r, name, repo_path, main_branch, recent_from_head)
                if _Piece.TIP_SUBJECT in pieces:
                    tip_subject = self._read_tip_subject(r, ahead)

                return _Visit(
                    status=status,
                    ahead=ahead,
                    behind=behind,
                    commit_graph=commit_graph,
                    recent_commits=recent_commits,
                    tip_subject=tip_subject,
                )
        except (git.InvalidGitRepositoryError, git.NoSuchPathError):
            return _Visit()

    def _read_status(self, r: git.Repo, name: str, repo_path: Path) -> _PorcelainStatus:
        # One porcelain-v2 call yields branch, upstream, tracking ahead/behind, and
        # every staged/unstaged/untracked path. `-z` keeps special-char paths
        # intact; `--untracked-files=all` matches GitPython's recursive listing.
        try:
            out = r.git.status("--porcelain=v2", "--branch", "--untracked-files=all", "-z")
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"status probe failed for {name}",
                cwd=repo_path,
            ) from exc
        return _parse_status_porcelain_v2(out)

    def _read_main_ahead_behind(
        self, r: git.Repo, name: str, repo_path: Path, main_branch: str | None
    ) -> tuple[int, int]:
        if not main_branch:
            return 0, 0
        main_ref = f"origin/{main_branch}"
        try:
            out = r.git.rev_list("--left-right", "--count", f"{main_ref}...HEAD")
        except git.GitCommandError as exc:
            # A missing `origin/<main>` (e.g. a brand-new clone with no fetch yet)
            # falls through to 0/0 instead of poisoning every refresh; verify the
            # ref explicitly so a real failure against a present ref still raises.
            if self._ref_present(r, main_ref):
                raise self._error_factory.from_git(
                    exc,
                    message=f"main-branch ahead/behind probe failed for {name}",
                    cwd=repo_path,
                ) from exc
            return 0, 0
        return _parse_main_ahead_behind(out)

    def _read_history(
        self,
        r: git.Repo,
        name: str,
        repo_path: Path,
        main_branch: str | None,
        recent_from_head: bool,
    ) -> tuple[list[str], list[RepoCommit]]:
        # Feature worktrees graph the divergence from origin/<main>; `--boundary`
        # surfaces the merge-base commit (marked `o`) so the history shows where
        # the branch left main. recent_commits is the non-boundary slice, capped at
        # 5. It follows the graph's topological order rather than the old
        # iter_commits date-order walk — invisible to the detail views, the only
        # remaining consumer (the `ws status` tip subject moved to the cheaper
        # `_read_tip_subject` probe and no longer reads this list).
        main_ref = f"origin/{main_branch}" if main_branch else None
        if main_ref is not None and not recent_from_head:
            try:
                out = r.git.log("--graph", "--decorate", "--boundary", _GRAPH_FORMAT, f"{main_ref}..HEAD")
            except git.GitCommandError as exc:
                # Missing `origin/<main>` (fresh clone, no fetch) is tolerated: the
                # graph falls back to HEAD and recent_commits stays empty, exactly
                # as the old split iter_commits / _build_commit_graph pair behaved.
                if self._ref_present(r, main_ref):
                    raise self._error_factory.from_git(
                        exc,
                        message=f"commit-history probe failed for {name}",
                        cwd=repo_path,
                    ) from exc
                return self._head_graph(r)[0], []
            graph_lines, commits = _parse_graph_log(out)
            return graph_lines, commits[:5]

        # HEAD graph: a standalone detail lists its tip commits (capped at 10); a
        # repo with no main branch graphs HEAD but emits an empty commit list
        # (recent_from_head is False when main_ref is absent).
        graph_lines, commits = self._head_graph(r)
        return graph_lines, (commits[:10] if recent_from_head else [])

    def _head_graph(self, r: git.Repo) -> tuple[list[str], list[RepoCommit]]:
        # Bounded HEAD graph, tolerant of an unborn HEAD (brand-new repo, no commits).
        try:
            out = r.git.log("--graph", "--decorate", "--max-count=30", _GRAPH_FORMAT, "HEAD")
        except git.GitCommandError:
            return [], []
        return _parse_graph_log(out)

    def _ref_present(self, r: git.Repo, ref: str) -> bool:
        try:
            r.git.rev_parse("--verify", "--quiet", ref)
            return True
        except git.GitCommandError:
            return False

    def get_ref_tip(self, worktree: FeatureWorktree, ref: str) -> str | None:
        """Full sha `ref` resolves to in the worktree's local object store, or
        None when it doesn't resolve there. No network, never raises — a
        missing worktree (unprovisioned, or removed on disk) reads the same
        as a ref absent from a present one, since both mean "nothing to
        report" for a caller deciding whether `ref` participates.
        """
        if not worktree.path.exists():
            return None
        try:
            with git.Repo(str(worktree.path)) as r:
                try:
                    return r.git.rev_parse("--verify", "--quiet", ref)
                except git.GitCommandError:
                    return None
        except (git.InvalidGitRepositoryError, git.NoSuchPathError):
            return None

    def is_ancestor(self, worktree: FeatureWorktree, ancestor_ref: str, ref: str) -> bool:
        """Whether `ancestor_ref` is reachable from `ref` — true also when the
        two resolve to the same commit, false for a sibling tip. `git
        merge-base --is-ancestor`, no network, never raises: an
        unresolvable ref reads as "not an ancestor" rather than an error,
        matching every other yes/no git probe in this class.
        """
        if not worktree.path.exists():
            return False
        try:
            with git.Repo(str(worktree.path)) as r:
                try:
                    r.git.merge_base("--is-ancestor", ancestor_ref, ref)
                    return True
                except git.GitCommandError:
                    return False
        except (git.InvalidGitRepositoryError, git.NoSuchPathError):
            return False

    def fork_point(self, worktree: FeatureWorktree, lower_ref: str, upper_ref: str) -> str | None:
        """Where `upper_ref` forked from `lower_ref`, read from `lower_ref`'s
        reflog — `git merge-base --fork-point <lower_ref> <upper_ref>`.

        The reflog remembers where `lower_ref` used to point even after a
        local rewrite (amend, squash, force-move) erases that commit from
        the graph, which is what makes this survive the rewrites this probe
        exists for. Returns None when no reflog entry qualifies — git exits
        non-zero with empty output in that case — never a wrong-but-plausible
        sha, and never raises.
        """
        if not worktree.path.exists():
            return None
        try:
            with git.Repo(str(worktree.path)) as r:
                try:
                    out = r.git.merge_base("--fork-point", lower_ref, upper_ref)
                except git.GitCommandError:
                    return None
                return out.strip() or None
        except (git.InvalidGitRepositoryError, git.NoSuchPathError):
            return None

    def is_rebase_in_progress(self, worktree: FeatureWorktree) -> bool:
        """Whether the worktree is currently stopped mid-rebase.

        Checked via the on-disk state directories git itself keeps for a
        rebase in progress — `rebase-merge` (the default backend since git
        2.26) and `rebase-apply` (the older one) — not `git status`, which
        carries no reliable porcelain field for this. No network; a missing
        worktree reads False.
        """
        if not worktree.path.exists():
            return False
        try:
            with git.Repo(str(worktree.path)) as r:
                for marker in ("rebase-merge", "rebase-apply"):
                    path = self._git_path(r, marker)
                    if path is not None and path.exists():
                        return True
                return False
        except (git.InvalidGitRepositoryError, git.NoSuchPathError):
            return False

    @staticmethod
    def _git_path(r: git.Repo, relative: str) -> Path | None:
        """Resolve `relative` under the worktree's *own* git-dir.

        `git rev-parse --git-path <relative>` resolves correctly for a
        linked worktree (`.git/worktrees/<name>/<relative>`), unlike
        constructing the path from `r.git_dir` by hand. Used both for the
        rebase-in-progress probe above and for reading a stopped rebase's
        conflict detail (`WriteRepoRepository._read_rebase_conflict`).
        """
        try:
            raw = r.git.rev_parse("--git-path", relative)
        except git.GitCommandError:
            return None
        path = Path(raw)
        if path.is_absolute():
            return path
        working_dir = r.working_dir
        return (Path(working_dir) if working_dir else Path.cwd()) / path

    def _read_tip_subject(self, r: git.Repo, ahead: int) -> str | None:
        # The minimal probe for `ws status`'s `last_commit_subject`. Preserves
        # the pre-refactor semantics — `recent_commits[0].message` from the
        # `origin/<main>..HEAD` history walk — without paying for that walk:
        # `ahead` (already computed by the STATUS piece this is always
        # requested alongside) is 0 whenever HEAD sits at parity with
        # `origin/<main>` or no main branch is configured, exactly the cases
        # the old history walk yielded an empty `recent_commits`. Only then is
        # HEAD's tip commit a candidate for `last_commit_subject`, so the `git
        # log -1` call is skipped entirely at parity. Tolerant of an unborn
        # HEAD (brand-new repo, no commits yet) the same way `_head_graph` is.
        if ahead == 0:
            return None
        try:
            out = r.git.log("-1", "--format=%s", "HEAD")
        except git.GitCommandError:
            return None
        subject = out.strip()
        return subject or None


# Typecheck-time conformance sentinel — Pyright rejects this return if
# ReadRepoRepository drifts from IReadRepoRepository. See
# winter-context:/architecture/repository-pattern.md.
def _conforms_read_repo_repository(x: ReadRepoRepository) -> IReadRepoRepository:
    return x
