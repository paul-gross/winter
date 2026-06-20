from __future__ import annotations

import stat

from winter_cli.core.filesystem import IFilesystemWriter
from winter_cli.modules.capability.spec_loader import ISpecLoader
from winter_cli.modules.ext.models import NewParams, ScaffoldResult

# Relative path inside the scaffolded extension directory for the entrypoint.
_ENTRYPOINT_REL = "workflow/service"


class ExtScaffoldService:
    """Scaffolds a new extension directory that passes `winter ext verify` out of the box.

    Given a name, a target capability slot, and an output directory, generates:
    - `winter-ext.toml` with the extension's name, `[provides]`, and `[implements]`.
    - `index.md` skeleton per harness extension-index convention.
    - An executable refuse-all stub entrypoint whose action set and exit codes
      are rendered from the loaded spec — ensuring scaffold↔verify parity.

    Refuses to overwrite a non-empty target directory unless `force=True`.
    """

    def __init__(
        self,
        spec_loader: ISpecLoader,
        fs: IFilesystemWriter,
    ) -> None:
        self._spec_loader = spec_loader
        self._fs = fs

    def scaffold(self, params: NewParams) -> ScaffoldResult:
        """Generate the extension skeleton. Returns a ScaffoldResult listing created paths."""
        out_dir = params.output_dir

        # Non-empty directory guard.
        if self._fs.is_dir(out_dir):
            contents = self._fs.iterdir(out_dir)
            if contents and not params.force:
                raise FileExistsError(
                    f"output directory {out_dir} already exists and is not empty; pass --force to overwrite"
                )

        # Pick the highest available version for the slot (same rule as verify_service).
        versions = sorted(self._spec_loader.supported_versions(params.slot))
        version = versions[-1]

        # Load the spec to source action names and exit codes.
        spec = self._spec_loader.load(params.slot, version)
        action_names = [a.name for a in spec.actions]
        unknown_exit = next((e.code for e in spec.exit_codes if "unknown" in e.meaning.lower()), 2)
        refuse_exit = next((e.code for e in spec.exit_codes if "refuse" in e.meaning.lower()), 3)

        # Render artifacts.
        manifest_content = self._render_manifest(params.name, _ENTRYPOINT_REL, version)
        index_content = self._render_index_md(params.name)
        stub_content = self._render_stub(action_names, unknown_exit, refuse_exit)

        # Paths.
        manifest_path = out_dir / "winter-ext.toml"
        index_path = out_dir / "index.md"
        entrypoint_path = out_dir / _ENTRYPOINT_REL

        # Write files.
        self._fs.mkdir(out_dir, parents=True, exist_ok=True)
        self._fs.mkdir(entrypoint_path.parent, parents=True, exist_ok=True)
        self._fs.write_text(manifest_path, manifest_content)
        self._fs.write_text(index_path, index_content)
        self._fs.write_text(entrypoint_path, stub_content)
        self._fs.chmod(entrypoint_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

        created = [manifest_path, index_path, entrypoint_path]
        return ScaffoldResult(output_dir=out_dir, created_files=created)

    @staticmethod
    def _render_manifest(name: str, entrypoint_rel: str, version: str) -> str:
        """Render the winter-ext.toml content for a scaffolded extension."""
        return f'name = "{name}"\n\n[provides]\nservice = "{entrypoint_rel}"\n\n[implements]\nservice = "{version}"\n'

    @staticmethod
    def _render_index_md(name: str) -> str:
        """Render the index.md skeleton per the harness extension-index convention.

        Minimal structure: title, path-notation block, and a "What this extension
        provides" section — exactly the workspace-runtime surface an agent needs.
        """
        return (
            f"# {name}\n"
            "\n"
            f"## Path notation\n"
            "\n"
            f"Files in this extension are addressed with the `{name}:` prefix —\n"
            f"for example, `{name}:/index.md`.\n"
            "\n"
            "## What this extension provides\n"
            "\n"
            f"*{name}* is a winter extension providing the `service` capability.\n"
            "Document its workspace-runtime rules and conventions here.\n"
        )

    @staticmethod
    def _render_stub(action_names: list[str], unknown_exit: int, refuse_exit: int) -> str:
        """Render a refuse-all stub entrypoint sourced from the loaded spec.

        The action set and both exit codes are parameters derived from the spec —
        not hard-coded here — so the stub and the verifier always agree.

        Protocol:
        - ``describe`` action → emits ``{"services": []}`` on stdout, exits 0
          (required by the ``emits-describe-json`` conformance check).
        - Other known actions → exit ``refuse_exit`` (recognized-but-unimplemented).
        - Unknown actions → exit ``unknown_exit``.
        - Echoes argv to stderr so the ``forwards-params`` check finds the sentinel.
        """
        lines = [
            "#!/usr/bin/env python3",
            '"""Refuse-all service stub — generated by `winter ext new`.',
            "",
            "Every declared action is recognized but returns the refuse-all exit code.",
            "Unknown actions return the unknown-action exit code.",
            'The describe action emits {"services": []} so emits-describe-json passes.',
            "Argv is echoed to stderr so `winter ext verify` forwards-params check passes.",
            '"""',
            "import sys",
            "",
            f"_KNOWN_ACTIONS = {{{', '.join(repr(a) for a in action_names)}}}",
            f"_UNKNOWN_EXIT = {unknown_exit}",
            f"_REFUSE_EXIT = {refuse_exit}",
            "",
            "print(' '.join(sys.argv), file=sys.stderr)",
            "",
            "if len(sys.argv) < 2:",
            "    sys.exit(_UNKNOWN_EXIT)",
            "",
            "action = sys.argv[1]",
            "if action not in _KNOWN_ACTIONS:",
            "    sys.exit(_UNKNOWN_EXIT)",
            "",
            '# describe must emit {"services": []} so the emits-describe-json check passes.',
            'if action == "describe":',
            "    print('{\"services\": []}')",
            "    sys.exit(0)",
            "",
            "sys.exit(_REFUSE_EXIT)",
        ]
        return "\n".join(lines) + "\n"
