"""Beads (``bd``) TaskBackend implementation."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from shipply.backends.beads_schema import (
    Bead,
    FormulaProto,
    Gate,
    Molecule,
    parse_beads_error,
    safe_bead,
    safe_beads,
    unwrap_beads_json,
)


class BeadsBackendError(Exception):
    """Raised when a Beads CLI invocation fails or returns unexpected data."""

    def __init__(
        self,
        message: str,
        command: list[str] | None = None,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.command = command or []
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.returncode is not None:
            parts.append(f"returncode={self.returncode}")
        if self.stderr:
            parts.append(f"stderr={self.stderr!r}")
        if self.stdout:
            parts.append(f"stdout={self.stdout!r}")
        return " ".join(parts)


class BeadsBackend:
    """Wrap the ``bd`` CLI, always using JSON output and list-style arguments.

    Parameters
    ----------
    binary:
        Path to the ``bd`` executable.  Defaults to ``"bd"``.
    actor:
        Actor name for the Beads audit trail.  Defaults to ``"shipply-forge"``.
    cwd:
        Working directory for subprocess invocations.  Defaults to the current
        working directory.
    env:
        Extra environment variables merged with the process environment.
        ``BD_JSON_ENVELOPE`` is always set to ``1`` to request the stable v2
        envelope shape.
    """

    _ID_RE = re.compile(r"^[A-Za-z0-9\-_/]+$")

    def __init__(
        self,
        binary: str = "bd",
        actor: str = "shipply-forge",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.binary = binary
        self.actor = actor
        self.cwd = str(cwd or os.getcwd())
        self.env = {**os.environ, "BD_JSON_ENVELOPE": "1"}
        if env:
            self.env.update(env)
        if self.actor and "BEADS_ACTOR" not in self.env:
            self.env["BEADS_ACTOR"] = self.actor

    def _validate_id(self, value: str) -> str:
        if not value or not self._ID_RE.match(value):
            raise BeadsBackendError(f"invalid Beads identifier: {value!r}")
        return value

    def _validate_path(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = Path(self.cwd) / path
        if not path.exists():
            raise BeadsBackendError(f"path not found: {value}")
        return path

    def _var_args(self, vars_: dict[str, Any]) -> list[str]:
        args: list[str] = []
        for key, value in vars_.items():
            self._validate_id(str(key))
            args.extend(["--var", f"{key}={value}"])
        return args

    async def _run(
        self,
        args: list[str],
        input_data: str | None = None,
        readonly: bool = False,
        sandbox: bool = True,
        expect_json: bool = True,
    ) -> Any:
        """Execute a ``bd`` command and return the parsed JSON payload."""
        cmd = [self.binary]
        if readonly:
            cmd.append("--readonly")
        if sandbox:
            cmd.append("--sandbox")
        if self.actor:
            cmd.extend(["--actor", self.actor])
        cmd.extend(args)
        if expect_json:
            cmd.append("--json")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.cwd,
                env=self.env,
                stdin=asyncio.subprocess.PIPE if input_data is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise BeadsBackendError(
                f"bd executable not found: {exc}",
                command=cmd,
            ) from exc
        stdout_bytes, stderr_bytes = await proc.communicate(
            input=input_data.encode("utf-8") if input_data is not None else None
        )
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            detail = parse_beads_error(stdout) or stderr or stdout
            raise BeadsBackendError(
                f"bd command failed: {detail}",
                command=cmd,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
            )

        if not expect_json:
            return stdout.strip()

        text = stdout.strip()
        if not text:
            return None

        try:
            data = unwrap_beads_json(text)
        except json.JSONDecodeError as exc:
            raise BeadsBackendError(
                f"invalid JSON from bd: {exc}",
                command=cmd,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
            ) from exc

        if isinstance(data, dict) and "error" in data:
            raise BeadsBackendError(
                f"bd command returned error: {data['error']}",
                command=cmd,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
            )

        return data

    # ------------------------------------------------------------------
    # Molecule creation
    # ------------------------------------------------------------------
    async def create_molecule(
        self, blueprint: dict[str, Any], proposal_id: str
    ) -> Molecule:
        """Create a molecule from a frozen Blueprint.

        Prefers, in order:

        1. A Beads formula on disk (``bd cook`` + ``bd mol pour``).
        2. A JSON plan graph (``bd create --graph``).
        3. Legacy ``bead_specs`` (create an epic root + child beads).
        """
        self._validate_id(proposal_id)

        formula_path = blueprint.get("formula_path")
        if formula_path:
            path = self._validate_path(str(formula_path))
            vars_ = blueprint.get("vars") or {}
            cook_args = ["cook", str(path), "--mode=runtime"]
            cook_args.extend(self._var_args(vars_))
            cooked = await self._run(cook_args)
            proto = FormulaProto.model_validate(cooked if isinstance(cooked, dict) else {})
            proto_id = proto.id or proto.formula or path.stem
            self._validate_id(proto_id)

            pour_args = ["mol", "pour", proto_id, "--assignee", self.actor]
            pour_args.extend(self._var_args(vars_))
            poured = await self._run(pour_args)
            return self._build_molecule(poured, proposal_id)

        plan_graph = blueprint.get("plan_graph")
        if plan_graph:
            graph_path = self._write_temp_json(plan_graph)
            try:
                created = await self._run(["create", "--graph", str(graph_path)])
                return self._build_molecule(created, proposal_id)
            finally:
                os.unlink(graph_path)

        bead_specs = blueprint.get("bead_specs")
        if bead_specs:
            return await self._create_legacy_molecule(bead_specs, blueprint, proposal_id)

        raise BeadsBackendError(
            "blueprint has no formula_path, plan_graph, or bead_specs"
        )

    def _build_molecule(self, data: Any, proposal_id: str) -> Molecule:
        """Build a :class:`Molecule` from the output of a create/pour command."""
        beads = safe_beads(data)
        if beads:
            root = beads[0]
            children = beads[1:]
            return Molecule(
                id=root.id,
                root_id=root.id,
                proposal_id=proposal_id,
                bead_ids=[b.id for b in children],
                beads=beads,
                title=root.title,
                status=root.status,
                issue_type=root.issue_type,
            )
        return Molecule(id="unknown", proposal_id=proposal_id)

    async def _create_legacy_molecule(
        self,
        bead_specs: list[dict[str, Any]],
        blueprint: dict[str, Any],
        proposal_id: str,
    ) -> Molecule:
        """Fallback path: create an epic root and attach each bead spec."""
        root_title = blueprint.get("title") or f"shipply-{proposal_id}"
        root = await self._run(
            ["create", root_title, "--type", "epic", "--assignee", self.actor]
        )
        root_bead = safe_bead(root)
        root_id = root_bead.id

        children: list[Bead] = []
        for spec in bead_specs:
            spec_id = spec.get("id")
            title = spec.get("title") or (f"Step {spec_id}" if spec_id else "Step")
            bead_type = spec.get("type") or "task"
            create_args = [
                "create",
                title,
                "--type",
                bead_type,
                "--parent",
                root_id,
                "--assignee",
                self.actor,
            ]
            description = spec.get("description")
            if description:
                create_args.extend(["--description", str(description)])
            created = await self._run(create_args)
            child = safe_bead(created)
            children.append(child)

            for dep in spec.get("needs", []):
                dep_id = self._resolve_dep_id(dep, children)
                if dep_id:
                    await self.add_dependency(dep_id, child.id)

        return Molecule(
            id=root_id,
            root_id=root_id,
            proposal_id=proposal_id,
            bead_ids=[c.id for c in children],
            beads=[root_bead, *children],
            title=root_bead.title,
        )

    def _resolve_dep_id(self, dep: Any, children: list[Bead]) -> str | None:
        if isinstance(dep, str):
            for child in children:
                if child.id == dep or child.title == dep:
                    return child.id
        return None

    def _write_temp_json(self, data: Any) -> str:
        """Write JSON data to a temporary file and return its path."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as tmp:
            json.dump(data, tmp)
            return tmp.name

    # ------------------------------------------------------------------
    # Bead lifecycle
    # ------------------------------------------------------------------
    async def get_ready(self, molecule_id: str) -> list[Bead]:
        """Return ready (claimable) beads within the molecule."""
        self._validate_id(molecule_id)
        data = await self._run(["ready", "--mol", molecule_id], readonly=True)
        return safe_beads(data)

    async def claim(self, bead_id: str) -> Bead:
        """Claim a specific bead atomically."""
        self._validate_id(bead_id)
        data = await self._run(["update", bead_id, "--claim"])
        return safe_bead(data)

    async def close(self, bead_id: str, reason: str | None = None) -> Bead:
        """Close a completed bead."""
        self._validate_id(bead_id)
        args = ["close", bead_id]
        if reason:
            args.extend(["--reason", reason])
        data = await self._run(args)
        return safe_bead(data)

    async def get_blocked(self, molecule_id: str) -> list[Bead]:
        """Return blocked beads within the molecule."""
        self._validate_id(molecule_id)
        data = await self._run(
            ["blocked", "--parent", molecule_id], readonly=True
        )
        return safe_beads(data)

    async def add_dependency(self, from_bead: str, to_bead: str) -> Any:
        """Create a dependency: ``to_bead`` depends on ``from_bead``."""
        self._validate_id(from_bead)
        self._validate_id(to_bead)
        return await self._run(
            ["dep", "add", to_bead, "--depends-on", from_bead]
        )

    async def get_bead(self, bead_id: str) -> Bead:
        """Fetch a single bead by ID (helper, not part of the Protocol)."""
        self._validate_id(bead_id)
        data = await self._run(["show", bead_id], readonly=True)
        return safe_bead(data)

    async def create_human_gate(self, bead_id: str, reason: str) -> Gate:
        """Create a human gate blocking a failed bead."""
        self._validate_id(bead_id)
        data = await self._run(
            [
                "gate",
                "create",
                "--type=human",
                "--blocks",
                bead_id,
                "--reason",
                reason,
            ]
        )
        return Gate.model_validate(data if isinstance(data, dict) else {})

    # ------------------------------------------------------------------
    # Root / sync
    # ------------------------------------------------------------------
    async def close_eligible_roots(self, molecule_id: str | None = None) -> list[str]:
        """Close molecule roots whose children are all complete.

        If ``molecule_id`` is provided, the returned list is filtered to that
        root (when it is closed by the command or already closed).
        """
        data = await self._run(["epic", "close-eligible"])
        closed = safe_beads(data)
        closed_ids = [b.id for b in closed]

        if molecule_id:
            self._validate_id(molecule_id)
            if molecule_id not in closed_ids:
                # Verify whether the root is already closed.
                root = await self.get_bead(molecule_id)
                if root.status == "closed":
                    closed_ids.append(molecule_id)
            return [mid for mid in closed_ids if mid == molecule_id]

        return closed_ids

    async def sync(self) -> None:
        """Push local Dolt state to a configured remote, if one exists.

        ``bd dolt push`` is only executed when ``bd dolt remote list`` reports
        at least one configured remote.
        """
        remote_list = await self._run(
            ["dolt", "remote", "list"], expect_json=False
        )
        text = str(remote_list).strip()
        if not text or "No remotes configured" in text:
            return
        await self._run(["dolt", "push"], expect_json=False)
