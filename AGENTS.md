# Project instructions

- This repository contains the standalone Classic World source reader, local index, and Skill. It has no runtime dependency on the Maple Atlas website.
- Keep client files read-only. Store client data and generated indexes in the user's separate local data directory and out of Git.
- Run an initial build or refresh only when the user requests it. Reuse supplied authorization and paths. Do not ask for the same authorization or path again.
- `status` and `query` read the saved snapshot only. They do not inspect the current client or refresh the index.
- For an existing-snapshot question, the Primary runs the known query entry point directly and answers from its results. Do not delegate individual lookups to subagents.
- Start with one bounded query and add focused detail or relation lookups only when needed for the answer. Do not start repository exploration, builds, refreshes, tests, or web research for a lookup. Run status only when snapshot usability is uncertain.
- The implemented schema 2 scope covers English source data for monsters, maps, equipment, items, skills, NPCs, quests, sets, and recipes. Use the checkpoint in `docs/DESIGN.md` for current acceptance status and observed source gaps.
- Preserve English source text, original IDs, raw values, and source evidence. Do not use a CN fallback or invent translated names.
- For player-facing answers, lead with practical advice and use familiar labels for understood attributes such as level, strength, or base attack. Keep English source text, IDs, raw values, and evidence intact internally; omit internal field names, IDs, and uninterpreted codes from routine answers unless requested or needed to locate a record. State uncertainty or missing optional fields only when they could change the recommendation. When character stats or equipment are unknown, state concise assumptions. Ordinary advice does not require combat measurements.
- Use explicit descriptions, stored skill levels, and prerequisite references to explain choices while preserving their original values. Do not invent meanings for undocumented codes, `reqJob` masks, `skillType`, `job_GL`, quest stage/state codes, or expressions.
- Do not infer an attack-speed category from raw `attackSpeed` without a confirmed mapping. Keep `incWAT` and `incPAD` distinct, never combine, substitute, or equate them, and disclose their separate values in routine advice only when both are present and relevant.
- Store only literal source relations. An unresolved relation remains visible as unresolved; it does not establish availability or game behavior.
- Do not add website access, model API calls, network requests, live data, background refresh, polling, watchers, or client-file writes.
- Keep upstream parser license and provenance notices with the vendored subset. See `docs/SOURCE_PROVENANCE.md`.
