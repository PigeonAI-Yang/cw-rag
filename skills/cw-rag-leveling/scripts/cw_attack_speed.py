"""Attack timing for Classic World weapons.

Turns a weapon's `attackSpeed` tag into a real attack interval in milliseconds,
so a leveling route can be expressed in seconds instead of hit counts.

Three inputs, and they are not equally solid:

1. Frame delays - **read from this project's client**, at
   ``Character/Character_000.wz -> 00002000.img``. Each action is a list of
   per-frame `delay` values in milliseconds. `read_client_frames.py` re-reads
   them from a client so this table can be regenerated and checked rather than
   trusted.

2. The per-frame scaling - from the meowdb community guide, so **candidate**,
   not a client listing:

       ScaledFrame    = trunc(FrameDelay * (10 + attackSpeed) / 16)
       nativeMs       = sum(ScaledFrame over the action's frames)
       AnimationTime  = 30 * ceil(nativeMs / 30)

   Two anchors make it more than a guess: at `attackSpeed` 6 the factor
   `(10 + 6) / 16` is exactly 1, so tier 6 is an identity, and the guide's own
   worked example (polearm at 8 -> 900 ms) reproduces from this table.

3. The swing/stab mix - a plain melee attack plays the weapon's ordinary
   actions, and every melee weapon image carries **three swing actions and two
   stabs**. So the mix is 60% swing / 40% stab, and that is a client fact
   (action counts), not an assumption.

A weapon family whose actions this table does not cover returns ``None``
instead of a made-up interval.
"""
from __future__ import annotations

import math

# Frame delays in milliseconds, read from the client's body image.
# `read_client_frames.py` regenerates this block from a client and fails when the
# two disagree, so a client update surfaces here instead of drifting silently.
FRAME_DELAYS: dict[str, list[int]] = {
    # Ordinary swings and stabs
    "swingO1": [300, 150, 350],
    "swingO2": [300, 150, 350],
    "swingO3": [300, 150, 350],
    "stabO1": [350, 450],
    "stabO2": [350, 450],
    "swingT1": [300, 150, 350],
    "swingT2": [300, 150, 350],
    "swingT3": [300, 150, 350],
    "stabT1": [300, 100, 350],
    "stabT2": [300, 100, 350],
    "swingP1": [300, 150, 350],
    "swingP2": [300, 150, 350],
    # The weapon families' finishing actions, as the client's images carry them.
    # These are not used by the ordinary-attack mix below, but they are real
    # client data and `read_client_frames.py` reports them.
    "swingOF": [200, 100, 100, 300],
    "swingTF": [200, 150, 150, 200],
    "swingPF": [100, 200, 200, 200],
    "stabOF": [250, 150, 300],
    "stabTF": [100, 200, 200, 200],
}

# Ordinary melee actions per weapon family, as the client's images carry them:
# three swings and two stabs for every melee class.
#
# key -> (item-id prefix, swing actions, stab actions)
FAMILIES: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "1h-sword": ("130", ("swingO1", "swingO2", "swingO3"), ("stabO1", "stabO2")),
    "1h-axe": ("131", ("swingO1", "swingO2", "swingO3"), ("stabO1", "stabO2")),
    "1h-blunt": ("132", ("swingO1", "swingO2", "swingO3"), ("stabO1", "stabO2")),
    "dagger": ("133", ("swingO1", "swingO2", "swingO3"), ("stabO1", "stabO2")),
    "2h-sword": ("140", ("swingT1", "swingT2", "swingT3"), ("stabO1", "stabO2")),
    "2h-axe": ("141", ("swingT1", "swingT2", "swingT3"), ("stabO1", "stabO2")),
    "2h-blunt": ("142", ("swingT1", "swingT2", "swingT3"), ("stabO1", "stabO2")),
    "spear": ("143", ("swingP1", "swingP2", "swingT2"), ("stabT1", "stabT2")),
    "polearm": ("144", ("swingP1", "swingP2", "swingT2"), ("stabT1", "stabT2")),
}

# The client's melee mix: three swing actions against two stabs.
SWING_SHARE = 0.6
STAB_SHARE = 0.4

SPEED_TAG_MIN = 2
SPEED_TAG_MAX = 8
TICK_MS = 30


def family_for_item_id(item_id: str) -> str | None:
    """Map a weapon item id to its animation family, or None if not melee."""
    if not item_id or len(item_id) < 3:
        return None
    prefix = item_id[:3]
    for family, (candidate, _, _) in FAMILIES.items():
        if candidate == prefix:
            return family
    return None


def action_ms(action: str, speed_tag: int) -> int | None:
    """One action's player-facing duration at the given attack-speed tag."""
    frames = FRAME_DELAYS.get(action)
    if not frames:
        return None
    if not isinstance(speed_tag, int) or not (SPEED_TAG_MIN <= speed_tag <= SPEED_TAG_MAX):
        return None
    # Per-frame scaling, then the 30 ms outer cadence (candidate rule; see module docstring).
    total = sum(math.trunc(frame * (10 + speed_tag) / 16) for frame in frames)
    return TICK_MS * math.ceil(total / TICK_MS)


def family_ms(family: str, speed_tag: int) -> tuple[float, float, float] | None:
    """(swing_ms, stab_ms, blended_ms) for a family at a speed tag, or None.

    The blended value is the 60/40 mix a plain melee attack actually plays.
    """
    entry = FAMILIES.get(family)
    if entry is None:
        return None
    _, swings, stabs = entry
    swing_values = [action_ms(a, speed_tag) for a in swings]
    stab_values = [action_ms(a, speed_tag) for a in stabs]
    if None in swing_values or None in stab_values:
        return None
    swing_avg = sum(swing_values) / len(swing_values)
    stab_avg = sum(stab_values) / len(stab_values)
    return swing_avg, stab_avg, SWING_SHARE * swing_avg + STAB_SHARE * stab_avg


def attack_interval_ms(item_id: str, speed_tag: int) -> float | None:
    """Blended ordinary-attack interval in ms for a weapon, or None if unknown."""
    family = family_for_item_id(item_id)
    if family is None:
        return None
    result = family_ms(family, speed_tag)
    if result is None:
        return None
    return result[2]


def attacks_per_second(item_id: str, speed_tag: int) -> float | None:
    interval = attack_interval_ms(item_id, speed_tag)
    if not interval or interval <= 0:
        return None
    return 1000.0 / interval
