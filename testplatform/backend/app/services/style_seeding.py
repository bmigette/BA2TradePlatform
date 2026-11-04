"""Stage-2 seeding: assemble one complete style individual from member contributions.

Pure functions over dicts (no DB, no launcher import). The launcher translates each member's
stage-1 genome into the style's gene keys (``ba2test_launcher._style_translate_key``) and builds
the authored genome (``strategy_param_space.authored_genome``); this module only decides, for
every gene of the style space, WHERE its value comes from, and refuses (naming the gene) when no
source may answer it. Plan: docs/plans/2026-11-04-stage2-jobA-seeding-and-gate-counter.md T5.
"""
import math
from fnmatch import fnmatch
from typing import Any, Dict, Iterable, List, Optional, Tuple

OTHER_MEMBERS_MODES = ("authored", "off")
OUT_OF_RANGE_MODES = ("clamp", "refuse")
#: A ``fill`` VALUE meaning "this gene's authored value": the policy's explicit, per-pattern opt-in for genes a
#: stage-1 source never had and that need a number but no decision (the threshold of a gate whose mode is filled
#: ``off``). Never applied unless a fill pattern names it.
FILL_AUTHORED = "$authored"


class SeedRefused(ValueError):
    """A seed that cannot be placed, answered or reproduced by the decoder. The message names the gene."""


def _is_rule_entry_toggle(key: str) -> bool:
    """``entry:<rid>:enabled`` -- the RULE-level toggle, not ``entry:<rid>:a<i>:enabled``."""
    parts = key.split(":")
    return len(parts) == 3 and parts[0] == "entry" and parts[2] == "enabled"


def _check_value(key: str, value: Any, cfg: Dict[str, Any], out_of_range: str, anchor: str,
                 clamped: List[Dict[str, Any]]) -> Any:
    """``value`` as a gene value the decoder can give back (decode-image rule of ``encode_params_strict``)."""
    from app.services.genetic import snap_to_lattice

    if cfg["type"] == "choice":
        if value not in cfg["choices"]:
            raise SeedRefused(f"{key!r}={value!r} is not one of {cfg['choices']!r}")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SeedRefused(f"{key!r}={value!r} is not a finite number")
    clipped = min(max(value, cfg["min"]), cfg["max"])
    back = snap_to_lattice(clipped, cfg, anchor)
    if abs(back - value) > 1e-9 * max(1.0, abs(value)):
        if out_of_range == "refuse":
            raise SeedRefused(f"{key!r}={value!r} cannot be decoded from [{cfg['min']}, {cfg['max']}] "
                              f"step {cfg.get('step')} (anchor {anchor!r}; nearest {back!r})")
        clamped.append({"key": key, "from": value, "to": back})
    return int(round(back)) if cfg["type"] == "int" else float(back)


def assemble_individual(space: Dict[str, Any], authored: Dict[str, Any],
                        member_keys: Dict[str, Iterable[str]], contributions: List[Dict[str, Any]], *,
                        shared_from: int = 0, other_members: str = "authored",
                        fill: Optional[Dict[str, Any]] = None, out_of_range: str = "clamp",
                        lattice_anchor: str = "zero") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """-> ``(genes, provenance)``; ``genes`` covers EXACTLY ``space``.

    ``member_keys``: ``{member: target gene keys that member owns}``; every other key of ``space`` is
    SHARED (``model:``, ``schedule:``, ``optsel:``, ``cond:shared-*``). ``contributions``: a list of
    ``{"member", "genes": {already-translated style key: value}, "label"}``. Resolution per key:

    1. a SEEDED member's key: its contribution; else (rule-level ``entry:<rid>:enabled`` only) ``1``;
       else ``fill`` (a glob -> value dict, consulted only for a key no contribution carries; the value
       ``FILL_AUTHORED`` means that gene's authored value); else refuse;
    2. a SHARED key: ``contributions[shared_from]`` if it has it, else the first contribution that does
       (disagreeing contributions are REPORTED in ``provenance["conflicts"]``), else ``fill``, else
       ``authored``, else refuse;
    3. an UN-SEEDED member's key: ``authored`` (its rule-level entry toggles are 0 when
       ``other_members == "off"``), else ``fill``, else refuse.

    Every value is checked against its gene (choice membership; numeric decode-image rule). Out of range
    or off-lattice: ``out_of_range="clamp"`` snaps ``clip(v)`` onto the lattice and records it in
    ``provenance["clamped"]``; ``"refuse"`` raises."""
    if other_members not in OTHER_MEMBERS_MODES:
        raise SeedRefused(f"other_members must be one of {OTHER_MEMBERS_MODES}, got {other_members!r}")
    if out_of_range not in OUT_OF_RANGE_MODES:
        raise SeedRefused(f"out_of_range must be one of {OUT_OF_RANGE_MODES}, got {out_of_range!r}")
    fill = dict(fill or {})

    owner: Dict[str, str] = {}
    for member, keys in member_keys.items():
        for k in keys:
            if k not in space:
                raise SeedRefused(f"member {member!r} owns {k!r}, which is not a gene of the space")
            if k in owner and owner[k] != member:
                raise SeedRefused(f"{k!r} is owned by both {owner[k]!r} and {member!r}")
            owner[k] = member

    seeded: Dict[str, int] = {}
    for i, c in enumerate(contributions):
        member = c["member"]
        if member not in member_keys:
            raise SeedRefused(f"contribution {i} is for unknown member {member!r}")
        if member in seeded:
            raise SeedRefused(f"member {member!r} has two contributions ({seeded[member]} and {i})")
        seeded[member] = i
        for k in c["genes"]:
            if k not in space:
                raise SeedRefused(f"contribution {i} ({c.get('label')}): {k!r} is not a gene of the space")
            if k in owner and owner[k] != member:
                raise SeedRefused(f"contribution {i} ({c.get('label')}): {k!r} belongs to member "
                                  f"{owner[k]!r}, not {member!r}")
    if contributions and not 0 <= shared_from < len(contributions):
        raise SeedRefused(f"shared_from={shared_from} is not a contribution index (0..{len(contributions) - 1})")

    def from_fill(key: str) -> Tuple[bool, Any]:
        for pat, v in fill.items():
            if fnmatch(key, pat):
                if v == FILL_AUTHORED:
                    if key not in authored:
                        raise SeedRefused(f"{key!r}: fill pattern {pat!r} asks for the authored value, "
                                          f"which the authored genome lacks")
                    return True, authored[key]
                return True, v
        return False, None

    genes: Dict[str, Any] = {}
    origin: Dict[str, str] = {}
    conflicts: List[Dict[str, Any]] = []
    clamped: List[Dict[str, Any]] = []

    for key, cfg in space.items():
        member = owner.get(key)
        if member is not None and member in seeded:
            i = seeded[member]
            cg = contributions[i]["genes"]
            if key in cg:
                value, org = cg[key], f"contribution:{i}"
            elif _is_rule_entry_toggle(key):
                value, org = 1, "toggle_on"
            else:
                ok, v = from_fill(key)
                if not ok:
                    raise SeedRefused(
                        f"seeded member {member!r}: {key!r} is absent from its contribution "
                        f"({contributions[i].get('label')}) and no fill pattern matches")
                value, org = v, "fill"
        elif member is not None:
            if other_members == "off" and _is_rule_entry_toggle(key):
                value, org = 0, "authored"
            elif key in authored:
                value, org = authored[key], "authored"
            else:
                ok, v = from_fill(key)
                if not ok:
                    raise SeedRefused(f"un-seeded member {member!r}: {key!r} has no authored value and no "
                                      f"fill pattern matches")
                value, org = v, "fill"
        else:
            having = [i for i, c in enumerate(contributions) if key in c["genes"]]
            if having:
                used = shared_from if shared_from in having else having[0]
                value, org = contributions[used]["genes"][key], f"shared:{used}"
                values = [contributions[i]["genes"][key] for i in having]
                if any(v != values[0] for v in values[1:]):
                    conflicts.append({"key": key, "values": values, "used": used})
            else:
                ok, v = from_fill(key)
                if ok:
                    value, org = v, "fill"
                elif key in authored:
                    value, org = authored[key], "authored"
                else:
                    raise SeedRefused(f"shared gene {key!r}: no contribution, fill pattern or authored value")
        try:
            genes[key] = _check_value(key, value, cfg, out_of_range, lattice_anchor, clamped)
        except SeedRefused as e:
            raise SeedRefused(f"{e} (origin {org})") from None
        origin[key] = org

    provenance = {"origin": origin, "conflicts": conflicts, "clamped": clamped,
                  "members": [c["member"] for c in contributions]}
    return genes, provenance
