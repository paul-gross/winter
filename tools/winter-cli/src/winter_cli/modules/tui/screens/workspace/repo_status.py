from __future__ import annotations

from rich.text import Text

from winter_cli.modules.workspace.models import WorktreeRepoStatus

def render_repo_cell(repo_status: WorktreeRepoStatus) -> Text:
    parts: list[tuple[str, str]] = []

    if repo_status.ahead > 0:
        parts.append((f"+{repo_status.ahead}", "green"))
    if repo_status.behind > 0:
        parts.append((f"-{repo_status.behind}", "yellow"))

    if repo_status.dirty_count == 1:
        parts.append(("1 file", "red"))
    elif repo_status.dirty_count > 1:
        parts.append((f"{repo_status.dirty_count} files", "red"))

    # `[+]` flags a non-pinned repo whose upstream is configured but the
    # remote-tracking ref doesn't exist locally yet — i.e., we're set up to
    # push to a feature branch that doesn't exist on origin, AND we actually
    # have commits the first push would carry across. Pinned repos follow
    # main and never participate in feature-branch flow; a repo with no
    # tracking config has nothing to flag; and a fresh worktree with no
    # commits ahead of main has nothing the marker would advertise.
    unborn_upstream = (
        not repo_status.worktree.repository.pinned
        and repo_status.tracking_branch is not None
        and not repo_status.tracking_ref_present
        and repo_status.ahead > 0
    )

    if len(parts) == 0 and not unborn_upstream and repo_status.tracking_ahead == 0:
        return Text("·", style="dim")

    text = Text()
    for i, (label, style) in enumerate(parts):
        if i > 0:
            text.append(" ")
        text.append(label, style=style)

    if repo_status.tracking_ahead > 0:
        prefix = " " if parts else ""
        text.append(f"{prefix}[+{repo_status.tracking_ahead}]", style="cyan")
    elif unborn_upstream:
        # Upstream configured but never fetched / never pushed. The bare
        # `[+]` (no count) reads as "ahead, by an unknown amount" alongside
        # the existing `[+N]` notation; orange flags it as a distinct state
        # — local config points somewhere that doesn't exist on the remote.
        prefix = " " if parts else ""
        text.append(f"{prefix}[+]", style="dark_orange")

    for key, value in repo_status.extensions.items():
        if key.startswith("_"):
            continue
        text.append(" ")
        if isinstance(value, Text):
            text.append(value)
        else:
            badge = str(value) if value else key
            text.append(badge, style="cyan")

    return text
