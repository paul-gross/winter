from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol

from winter_cli.config.models import CodeAgentVendor
from winter_cli.core.filesystem import IFilesystemWriter
from winter_cli.modules.workspace.models import RepoError


class SymlinkInstaller:
    """Creates and prunes the relative `<prefix>-*` symlinks winter projects.

    The low-level symlink primitives shared by the symlink skill strategy
    (below) and agent installation (`ExtensionSymlinkService`). A service class
    rather than free functions so the `IFilesystemWriter` seam is injected,
    not threaded through call after call.
    """

    def __init__(self, fs: IFilesystemWriter) -> None:
        self._fs = fs

    def install_entries(
        self,
        *,
        source_root: Path | None,
        target_root: Path,
        prefix: str,
        kind: str,
        include_dirs: bool,
        include_files: bool,
        file_suffix: str = "",
        exclude_filenames: tuple[str, ...] = (),
        require_marker_file: str | None = None,
    ) -> list[str]:
        """Create one symlink per matching entry in `source_root`.

        For directory entries the symlink keeps the directory name (`<prefix>-<dirname>`).
        For file entries with a matching suffix the symlink keeps the full filename
        (`<prefix>-<filename>`), so a `.md` extension is preserved.

        `exclude_filenames` skips matching file entries by exact basename — used to
        keep `README.md` out of the installed agent set. `require_marker_file`
        restricts directory entries to those containing that marker file (e.g.
        `SKILL.md`, `AGENT.md`), so doc-only subdirectories don't masquerade as
        skills or nested agents.

        Returns the list of created/existing symlink names. Empty when `source_root`
        is None or doesn't exist. Raises `RepoError` on conflict or I/O failure.
        """
        if source_root is None or not self._fs.is_dir(source_root):
            return []

        self._fs.mkdir(target_root, parents=True, exist_ok=True)

        linked: list[str] = []
        for entry in sorted(self._fs.iterdir(source_root)):
            if self._fs.is_dir(entry):
                if not include_dirs:
                    continue
                if require_marker_file is not None and not self._fs.is_file(entry / require_marker_file):
                    continue
                link_name = f"{prefix}-{entry.name}"
            elif self._fs.is_file(entry):
                if not include_files:
                    continue
                if file_suffix and not entry.name.endswith(file_suffix):
                    continue
                if entry.name in exclude_filenames:
                    continue
                link_name = f"{prefix}-{entry.name}"
            else:
                continue

            link_path = target_root / link_name
            target = self._relative_target(target_root, entry)

            if self._fs.is_symlink(link_path):
                # Update if pointing at the wrong place.
                try:
                    current = self._fs.readlink(link_path)
                except OSError:
                    current = None
                if current != target:
                    try:
                        self._fs.unlink(link_path)
                        self._fs.symlink_to(link_path, target)
                    except OSError as exc:
                        raise RepoError(f"refresh {kind} symlink {link_name}: {exc}") from exc
                linked.append(link_name)
                continue

            if self._fs.exists(link_path):
                raise RepoError(
                    f"cannot create {kind} symlink {link_name}: path exists and is not a symlink",
                )

            try:
                self._fs.symlink_to(link_path, target)
            except OSError as exc:
                raise RepoError(f"create {kind} symlink {link_name}: {exc}") from exc
            linked.append(link_name)

        return linked

    def prune_stale(
        self,
        target_root: Path,
        prefix: str,
        live_names: set[str],
        kind: str,
    ) -> None:
        """Remove any `<prefix>-*` symlinks in `target_root` that weren't created this pass.

        Catches two cases:
          - the source entry was deleted upstream (broken symlink like the
            historical `wf-blizzard` after the source `agents/blizzard/`
            directory went away);
          - the source entry still exists but is now filtered out by the
            install pass (README.md, AGENT.md-less directories).

        Only symlinks whose name starts with `f"{prefix}-"` are considered —
        each extension owns its prefix, so this won't touch other extensions'
        links or user-placed files. Raises `RepoError` on I/O failure.
        """
        if not self._fs.is_dir(target_root):
            return

        prefix_with_dash = f"{prefix}-"
        for entry in sorted(self._fs.iterdir(target_root)):
            if not self._fs.is_symlink(entry):
                continue
            if not entry.name.startswith(prefix_with_dash):
                continue
            if entry.name in live_names:
                continue
            try:
                self._fs.unlink(entry)
            except OSError as exc:
                raise RepoError(f"prune stale {kind} symlink {entry.name}: {exc}") from exc

    @staticmethod
    def _relative_target(link_dir: Path, target: Path) -> Path:
        """Compute the symlink target as a path relative to the link's parent directory.

        Relative targets keep the workspace portable — moving the workspace doesn't
        invalidate the links.
        """
        return Path(os.path.relpath(target, link_dir))


# ── Skill install strategies ───────────────────────────────────────────────


class InstallSkillStrategy(Protocol):
    """Installs an extension's skill directories into one vendor's skills dir.

    `install` materializes every skill directory (a directory containing a
    `SKILL.md` marker) under `source_root` into `target_root` as
    `<prefix>-<dirname>`, prunes stale `<prefix>-*` entries with no live
    source, and returns the list of installed names. Concrete strategies differ
    only in the materialization mechanism (symlink vs copy).
    """

    def install(self, *, source_root: Path | None, target_root: Path, prefix: str) -> list[str]: ...


class SymlinkSkillStrategy:
    """Projects skills as relative directory symlinks (ClaudeCode, Codex)."""

    def __init__(self, fs: IFilesystemWriter) -> None:
        self._symlinks = SymlinkInstaller(fs)

    def install(self, *, source_root: Path | None, target_root: Path, prefix: str) -> list[str]:
        names = self._symlinks.install_entries(
            source_root=source_root,
            target_root=target_root,
            prefix=prefix,
            kind="skill",
            include_dirs=True,
            include_files=False,
            require_marker_file="SKILL.md",
        )
        self._symlinks.prune_stale(target_root, prefix, set(names), kind="skill")
        return names


class CopySkillStrategy:
    """Materializes skills as real directory copies (OpenCode).

    OpenCode globs `skill/**/SKILL.md` and does not traverse symlinked
    directories, so it needs real directories under `.opencode/skill/`.

    Idempotency is by runtime content hash, with nothing persisted: on each
    install the source and destination trees are hashed and compared. Equal
    means the destination is already current and is left untouched; a mismatch
    (or a missing destination) triggers a delete-then-copy. Stale `<prefix>-*`
    destinations with no live source are removed.
    """

    def __init__(self, fs: IFilesystemWriter, vendor: CodeAgentVendor) -> None:
        self._fs = fs
        self._transforms = CopiedSkillTransformPipeline.for_vendor(vendor)

    def install(self, *, source_root: Path | None, target_root: Path, prefix: str) -> list[str]:
        installed: list[str] = []
        if source_root is not None and self._fs.is_dir(source_root):
            self._fs.mkdir(target_root, parents=True, exist_ok=True)
            for entry in sorted(self._fs.iterdir(source_root)):
                if not self._fs.is_dir(entry):
                    continue
                if not self._fs.is_file(entry / "SKILL.md"):
                    continue
                name = f"{prefix}-{entry.name}"
                self._sync(entry, target_root / name, skill_name=name)
                installed.append(name)

        self._prune(target_root, prefix, set(installed))
        return installed

    def content_hash(self, root: Path, *, skill_name: str | None = None) -> str:
        """Return the deterministic SHA-256 content hash for a skill directory tree.

        Accepts an ``IFilesystemReader`` (``self._fs`` is typed as writer but is a
        superset in practice). Both the installer (``_sync``) and
        ``SkillProbeService._check_copy`` call this method so the hash computation
        is shared in one place. Pass ``skill_name`` when hashing a source tree so
        vendor transforms are applied consistently; omit it (or pass ``None``) when
        hashing an already-installed destination tree.
        """
        return self._hash_tree(root, skill_name=skill_name)

    def _sync(self, source_dir: Path, dest_dir: Path, *, skill_name: str) -> None:
        """Copy `source_dir` to `dest_dir`, skipping the copy when content matches."""
        dest_present = self._fs.is_dir(dest_dir)
        if dest_present and self._hash_tree(source_dir, skill_name=skill_name) == self._hash_tree(dest_dir):
            return
        try:
            if dest_present:
                self._fs.rmtree(dest_dir)
            self._fs.copytree(source_dir, dest_dir)
            self._apply_transforms(dest_dir, skill_name=skill_name)
        except OSError as exc:
            raise RepoError(f"copy skill {dest_dir.name}: {exc}") from exc

    def _apply_transforms(self, dest_dir: Path, *, skill_name: str) -> None:
        skill_md = dest_dir / "SKILL.md"
        text = self._fs.read_text(skill_md)
        transformed = self._transforms.apply(skill_md.relative_to(dest_dir), text, skill_name=skill_name)
        if transformed != text:
            self._fs.write_text(skill_md, transformed)

    def _prune(self, target_root: Path, prefix: str, live_names: set[str]) -> None:
        """Remove `<prefix>-*` destination directories with no live source."""
        if not self._fs.is_dir(target_root):
            return
        prefix_with_dash = f"{prefix}-"
        for entry in sorted(self._fs.iterdir(target_root)):
            if not self._fs.is_dir(entry):
                continue
            if not entry.name.startswith(prefix_with_dash):
                continue
            if entry.name in live_names:
                continue
            try:
                self._fs.rmtree(entry)
            except OSError as exc:
                raise RepoError(f"prune stale skill copy {entry.name}: {exc}") from exc

    def _hash_tree(self, root: Path, *, skill_name: str | None = None) -> str:
        """Deterministically hash the file contents of a directory tree.

        Walks every file under `root` in a stable order and folds each file's
        root-relative path and bytes into one SHA-256 digest. The hash is over
        content relative to `root`, so two trees with identical files but
        different root directory names (e.g. `do-thing` vs `wf-do-thing`) hash
        equal — exactly the comparison the copy needs across a rename.
        Recomputed from scratch each call; nothing is persisted.
        """
        files: list[tuple[str, bytes]] = []
        self._collect_files(root, Path("."), files, skill_name=skill_name)
        files.sort(key=lambda item: item[0])

        digest = hashlib.sha256()
        for rel, data in files:
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(len(data)).encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
        return digest.hexdigest()

    def _collect_files(
        self,
        current: Path,
        rel: Path,
        out: list[tuple[str, bytes]],
        *,
        skill_name: str | None,
    ) -> None:
        for entry in sorted(self._fs.iterdir(current)):
            entry_rel = rel / entry.name
            if self._fs.is_dir(entry):
                self._collect_files(entry, entry_rel, out, skill_name=skill_name)
            elif self._fs.is_file(entry):
                if skill_name is not None and entry_rel == Path("SKILL.md"):
                    text = self._fs.read_text(entry)
                    transformed = self._transforms.apply(entry_rel, text, skill_name=skill_name)
                    out.append((entry_rel.as_posix(), transformed.encode()))
                else:
                    out.append((entry_rel.as_posix(), self._fs.read_bytes(entry)))


class CopiedSkillTransformPipeline:
    """Applies vendor-specific transforms to copied skill files."""

    def __init__(self, transforms: tuple[ICopiedSkillTransform, ...]) -> None:
        self._transforms = transforms

    @classmethod
    def for_vendor(cls, vendor: CodeAgentVendor) -> CopiedSkillTransformPipeline:
        if vendor is CodeAgentVendor.OpenCode:
            return cls((OpenCodeSkillNameTransform(),))
        return cls(())

    def apply(self, rel_path: Path, text: str, *, skill_name: str) -> str:
        for transform in self._transforms:
            text = transform.apply(rel_path, text, skill_name=skill_name)
        return text


class ICopiedSkillTransform(Protocol):
    def apply(self, rel_path: Path, text: str, *, skill_name: str) -> str: ...


class OpenCodeSkillNameTransform:
    """Set copied OpenCode SKILL.md frontmatter `name` to its installed directory."""

    def apply(self, rel_path: Path, text: str, *, skill_name: str) -> str:
        if rel_path != Path("SKILL.md"):
            return text
        if not text.startswith("---"):
            return f"---\nname: {skill_name}\n---\n\n{text}"

        lines = text.split("\n")
        end_idx = None
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end_idx = i
                break
        if end_idx is None:
            return text

        for i in range(1, end_idx):
            if lines[i].strip().startswith("name:"):
                lines[i] = f"name: {skill_name}"
                return "\n".join(lines)

        lines.insert(1, f"name: {skill_name}")
        return "\n".join(lines)


# One sentinel per Protocol/adapter pair (winter-harness:/standards/protocol-conformance.md):
# both strategies must satisfy InstallSkillStrategy independent of the factory site.
def _conforms_symlink_skill_strategy(x: SymlinkSkillStrategy) -> InstallSkillStrategy:
    return x


def _conforms_copy_skill_strategy(x: CopySkillStrategy) -> InstallSkillStrategy:
    return x
