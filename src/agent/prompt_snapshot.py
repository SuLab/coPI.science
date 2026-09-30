"""What the engine loaded at start: the prompt set, the specialist personas,
each role's manifest and ``load_role`` result, and the rubric (spec §8.6, B16).

Loaded once per engine start and installed process-wide; the prompt readers
(``Agent._load_prompt``, the consult persona read, ``tools_for_role``, the
tool gate, the phase-5 menu and the engine's ``deps.load_role``) read from it,
so a mid-run edit of a role prompt, persona or ``role.toml`` cannot change the
next turn — it raises the drift flag instead (the heartbeat calls
``disk_drift`` every 60 s). The rubric is NOT re-read: the snapshot holds the
import-time ``load_rubric()`` object that scoring already uses, so prompts,
stage bars, ``weighted_score``, bands and stamps keep sharing one rubric.

Read-only: nothing here writes a prompt or rubric file (C27). With no snapshot
installed (the web app, the worker, most tests) every reader keeps reading the
disk exactly as before.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from src.agent import roles as _roles
from src.agent import specialists as _specialists
from src.agent.role_capabilities import ROLE_CAPABILITIES
from src.agent.roles import PromptSetStamp, RoleSpec
from src.services import blackbird_rubric as _rubric


def _hash12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


@dataclass(frozen=True)
class PromptSnapshot:
    #: (role, filename) -> text; None when the file was missing, so the caller's
    #: built-in default applies exactly as ``Agent._load_file`` does today.
    prompts: dict[tuple[str, str], str | None]
    manifests: dict[str, bytes | None]
    role_specs: dict[str, RoleSpec]
    #: domain -> persona text; None when the persona file was missing.
    personas: dict[str, str | None]
    rubric: Any
    stamps: dict[str, PromptSetStamp]
    persona_hashes: dict[str, str | None]

    @classmethod
    def load(cls) -> PromptSnapshot:
        prompts: dict[tuple[str, str], str | None] = {}
        manifests: dict[str, bytes | None] = {}
        specs: dict[str, RoleSpec] = {}
        stamps: dict[str, PromptSetStamp] = {}
        for role in _roles.available_roles():
            for filename in ROLE_CAPABILITIES[role].prompt_files:
                path = _roles.resolve_prompt_path(role, filename)
                try:
                    prompts[(role, filename)] = path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    prompts[(role, filename)] = None
            manifest = _roles.ROLES_DIR / role / "role.toml"
            manifests[role] = manifest.read_bytes() if manifest.is_file() else None
            specs[role] = _roles.load_role(role)
            stamps[role] = _roles.prompt_set_stamp(role)
        personas: dict[str, str | None] = {}
        persona_hashes: dict[str, str | None] = {}
        for domain in sorted(_specialists.SPECIALIST_DOMAINS):
            path = _specialists.persona_path(domain)
            if path.is_file():
                # read_text, like the consult's disk read, so newline handling
                # (and so the prompt bytes) is identical.
                personas[domain] = path.read_text(encoding="utf-8")
                persona_hashes[domain] = _hash12(path.read_bytes())
            else:
                personas[domain] = None
                persona_hashes[domain] = None
        return cls(
            prompts=prompts, manifests=manifests, role_specs=specs, personas=personas,
            rubric=_rubric.load_rubric(), stamps=stamps, persona_hashes=persona_hashes,
        )

    def prompt_text(self, role: str, filename: str) -> tuple[bool, str | None]:
        """``(known, text)``: ``known`` False for a role/file the snapshot did not
        load (the caller then reads the disk, as today)."""
        key = (role, filename)
        return (key in self.prompts, self.prompts.get(key))

    def persona(self, domain: str) -> tuple[bool, str | None]:
        return (domain in self.personas, self.personas.get(domain))

    def role_spec(self, role: str) -> RoleSpec | None:
        return self.role_specs.get(role)

    def stamps_json(self) -> dict[str, dict[str, str]]:
        return {
            role: {"version": s.version, "content_hash": s.content_hash}
            for role, s in self.stamps.items()
        }

    def disk_drift(self) -> dict[str, Any]:
        """What changed on disk since load: ``{"prompt_drift": {name: {"loaded",
        "on_disk"}}, "rubric": {"loaded", "on_disk"}}``. Names are role names,
        ``persona:<domain>`` and ``rubric``."""
        drift: dict[str, dict[str, str | None]] = {}
        for role, loaded in self.stamps.items():
            now = _roles.prompt_set_stamp(role)
            if now.content_hash != loaded.content_hash:
                drift[role] = {"loaded": loaded.content_hash, "on_disk": now.content_hash}
        for domain, loaded in self.persona_hashes.items():
            path = _specialists.persona_path(domain)
            now = _hash12(path.read_bytes()) if path.is_file() else None
            if now != loaded:
                drift[f"persona:{domain}"] = {"loaded": loaded, "on_disk": now}
        try:
            on_disk = _hash12(_rubric.RUBRIC_PATH.read_bytes())
        except OSError:
            on_disk = None
        if on_disk != self.rubric.content_hash:
            drift["rubric"] = {"loaded": self.rubric.content_hash, "on_disk": on_disk}
        return {
            "prompt_drift": drift,
            "rubric": {"loaded": self.rubric.content_hash, "on_disk": on_disk},
        }


_ACTIVE: PromptSnapshot | None = None


def install(snapshot: PromptSnapshot | None) -> None:
    """Install (or, with None, remove) the process-wide snapshot."""
    global _ACTIVE
    _ACTIVE = snapshot


def active() -> PromptSnapshot | None:
    return _ACTIVE


def role_spec(role: str) -> RoleSpec:
    """``load_role(role)`` as loaded at engine start when a snapshot is
    installed and knows the role; otherwise today's disk read."""
    snapshot = _ACTIVE
    spec = snapshot.role_spec(role) if snapshot is not None else None
    return spec if spec is not None else _roles.load_role(role)
