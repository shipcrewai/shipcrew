"""TaskBackend protocol for Shipply execution backends."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from shipply.backends.beads_schema import Bead, Molecule


@runtime_checkable
class TaskBackend(Protocol):
    """Abstract execution backend for turning a Blueprint into tracked work."""

    async def create_molecule(
        self, blueprint: dict[str, Any], proposal_id: str
    ) -> Molecule:
        """Create a molecule (root + children) from a frozen Blueprint."""
        ...

    async def get_ready(self, molecule_id: str) -> list[Bead]:
        """Return claimable beads within the molecule."""
        ...

    async def claim(self, bead_id: str) -> Bead:
        """Atomically claim a bead for execution."""
        ...

    async def close(self, bead_id: str, reason: str | None = None) -> Bead:
        """Close a completed bead."""
        ...

    async def get_blocked(self, molecule_id: str) -> list[Bead]:
        """Return beads blocked in the molecule."""
        ...

    async def add_dependency(self, from_bead: str, to_bead: str) -> Any:
        """Make ``to_bead`` depend on ``from_bead``."""
        ...

    async def close_eligible_roots(self, molecule_id: str | None = None) -> list[str]:
        """Close molecule roots whose children are complete.

        Returns the IDs of roots that were closed.
        """
        ...

    async def sync(self) -> None:
        """Push local state to a configured remote, if any."""
        ...
