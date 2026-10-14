# Changelog

All notable changes to CW RAG. Version numbers follow semantic versioning.

## 1.0.0 - 2026-10-15

First release. The existing local-snapshot retrieval work and the leveling child skill ship together as 1.0.0.

### Features

- **Local CW client snapshot retrieval**: parses a Classic World client into a read-only SQLite snapshot and queries monsters, maps, equipment, items, skills, NPCs, quests, sets and recipes, plus literal map-to-monster static relations.
- **Leveling child skill `cw-rag-leveling`**: ranks hunting targets by **experience per second**. The headline figure is `exp per kill / seconds per kill`, so "how many hits" becomes "how many seconds".
- **Real attack intervals**: reads every attack action's per-frame delay from `Character/Character_000.wz/00002000.img` and derives swing time - speed 4 is about 720 ms, speed 8 about 900 ms. An ordinary melee attack is 60% swing and 40% stab, a mix taken from the client, where every melee weapon image carries three swing actions and two stabs.
- **Frame-delay re-extraction script**: `read_client_frames.py` prints the per-frame delays, compares them with the built-in table, and exits non-zero when they disagree, so a client update can be checked directly.
- **Monster defence scaling**: applies `effective = damage * 100 / (PDDamage + 100)`, so PDDamage 100 halves a hit and PDDamage 500 leaves a sixth.
- **Browser calculator**: `web/cw-calculator.html` mirrors the CLI, with experience per second, seconds per kill and a defence column, plus a switch for the defence scaling.

### Fixes

- Removed the automatic damage estimate. An earlier version estimated damage when none was supplied and predicted a 115 ceiling where the measurement was 77 - **overstating by 50%**, which produced a wrong ranking. A missing measurement is now an error.
- Removed the attack-speed percentage estimate. An earlier version assumed 12% per speed tag, turning a real **23%** spread between the fastest and slowest weapons into **48%**, and concluded from it that a spear swings far slower than an axe. With the client's own frame delays the conclusion reverses: a spear's output is about 20% higher.
- Corrected how the defence field is used. It was once displayed without affecting the calculation, which understated hit counts for monsters that carry defence.

### Documentation

- Both READMEs gained the child-skill section and an accurate description of the leveling capability.
- The child skill's SKILL.md now states one rule - extract, do not estimate - and grades every input as read from the client, candidate, measured, or absent.

### Tests

- 139 tests pass, 80 of them for the leveling child skill. They cover both attack-speed anchors (speed 6 is an identity, polearm at speed 8 is 900 ms), monotonicity across speed tags, the 60/40 blend, the defence arithmetic and its identity at zero defence, the experience-per-second ranking, and the refusal to run without a measured damage value.
- Every test runs on synthetic snapshots and needs no real client.

### Known limits

- The damage formula is not in the client, so per-hit damage must be measured by the player.
- The defence rule comes from a community guide rather than a client listing and is labelled a candidate. It can be switched off, or verified by hitting a PDDamage 30 monster and reading the damage ceiling.
- The frame-delay scaling rule also comes from a community guide. Its inputs - frame delays and speed tags - are read from the client, and two anchors check it.
- The snapshot contains no drop rates, server rates, or event bonuses.
