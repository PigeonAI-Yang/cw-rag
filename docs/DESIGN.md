# Classic World local retrieval contract

This document records the accepted schema 2 implementation contract and final status. The implementation passed the authorized 59-test suite and real-client build/query acceptance; observed counts, evidence, and source-bound gaps appear in the Current Goal checkpoint. The inspected client was `J:/nexon/maplestorycw/appdata/Data`.

The tool remains standalone Python with a vendored WZ/MS parser and `pycryptodome`. It reads one existing Classic World client and writes one separate local SQLite snapshot. It preserves English source text, original IDs, and source evidence. There is no website dependency, CN fallback, model API, network request, vector index, background refresh, or client-file write. Icons, audio, live drops, server rates, combat formulas, and quest script execution remain outside scope.

## Evidence and source selection

The implementation basis is `scripts/cw_source.py`, `scripts/cw_rag.py`, and `tests/test_cw_rag.py`. Reuse references are `J:/PigeonYang/sites/maple-atlas/scripts/classic_world.py`, especially its String, metadata, MS, QuestData, and Etc readers, and `static_metadata.py` for source families and grouped recipes. CW RAG must not import that website project at runtime.

The current source is `J:/nexon/maplestorycw/appdata/Data`. Raw evidence under `.cw-rag/contract-evidence/` supplies these additional facts:

- `items.json` records weapon IDs `01302000` and `01302001` with `info/incWAT` values 17 and 27. `incWAT` must remain distinct from `incPAD`. Consume records `02000000` through `02000002` are children of `0200.img`, with `spec/hp` values 100, 250, and 500.
- The same evidence records valid 13-byte `ItemMake.img` and `SetItemInfo.img` scalar trees that are empty. They require `source_present_empty` coverage with source hashes and zero records. They do not justify invented recipes, sets, or reference semantics.
- `skills.json` records skill groups 100 and 200, explicit `level` entries, mixed integer/string values, `hs` strings, and `req` maps such as `1000000: 3`. No common expression was observed in those six sampled skills.
- `quests.json` records quests 1000 through 1011, numeric Check/Act/Say stages, NPC/item/mob/quest references, signed item counts, `nextQuest`, and raw `job_GL`. NPC samples contain `info/speak`. Stage codes and `job_GL` have no verified gameplay interpretation.

Schema 2 selects these resources, excluding every `_Canvas` directory:

| Source | Retained data |
| --- | --- |
| `String/String_*.wz` | English names, descriptions, and skill book/level strings |
| `Packs/Mob_*.ms`, `Packs/Skill_*.ms` | Monster and skill definitions |
| `Map/Map/**/*_*.wz` | Map info, life placements, and portals |
| `Character/**/*_*.wz`, `Item/**/*_*.wz` | Individual and grouped equipment/item definitions |
| `Npc/**/*_*.wz` | NPC static scalar data |
| `Quest/QuestData/QuestData_*.wz` | QuestInfo, Check, Act, and Say |
| `Etc/*_*.wz` | Only `SetItemInfo.img` and `ItemMake.img` are parsed |

The selector fingerprints every selected resource. Existing String roots, the ClassicWorld marker in Eqp, and monster/map identity checks remain mandatory. An absent additional source set or image produces explicit coverage. Valid empty selected images are accepted and reported as empty. A malformed, unreadable, truncated, or conflicting selected source fails the build and preserves the previous snapshot. Missing, empty, and malformed are distinct outcomes.

## Identity, fields, and provenance

An entity key is `(kind, normalized_id)`. Kinds are `mob`, `map`, `equipment`, `item`, `skill`, `npc`, `quest`, `set`, and `recipe`.

Numeric IDs normalize to decimal text without leading zeroes. Existing mob/map limits remain seven and nine significant digits. Other numeric kinds accept up to 20 ASCII digits and remain text in SQLite. Recipe IDs use the normalized two-part path `group/entry`, because ItemMake groups can contain the same entry key. Each component has the same 20-digit bound. `original_id` preserves the original ID or both recipe path components. An entry ID alone is not a unique recipe identity.

Equipment uses Character families `Cap`, `Coat`, `Longcoat`, `Pants`, `Shoes`, `Glove`, `Cape`, `Weapon`, `Shield`, `Accessory`, `Ring`, `PetEquip`, `TamingMob`, and `Dragon`. This records a source family, not a claim that a character can wear the object. `Face`, `Hair`, and `Morph` use `item`. Item-directory families include `Consume`, `Etc`, `Install`, `Cash`, `Special`, and `Pet`. Unknown families remain `item` with their original family and an `unclassified_family` gap. No family is silently discarded.

Every entity has this outer shape:

```python
{
    "kind": str,
    "normalized_id": str,
    "original_id": str,
    "name": str | None,
    "fields": dict,
    "sources": list[dict],
}
```

The fields contract is:

| Key | Shape |
| --- | --- |
| `name_info` | Original String properties other than the chosen name |
| `static_info` | All original scalar `info/...` paths, not the old whitelist |
| `family` | Original source family string |
| `section` | `equipment` for equipment kinds; `appearance` for Face/Hair/Morph; `pets` for Item/Pet; otherwise `items` |
| `item_effects` | Flat original `spec/...` properties |
| `skill_static` | Nested scalar tree of one skill, preserving common/level/req and all original keys |
| `skill_resource_group` | Normalized numeric image stem; not a job category |
| `skill_book` | `{group: str, title: str}` from observed String bookName |
| `quest_info`, `quest_check`, `quest_act`, `quest_say` | Separate nested source subtrees, with original phase/index keys |
| `set_data`, `recipe_data` | Nested scalar subtree for one set or recipe |
| `recipe_group`, `recipe_entry` | Original recipe path components |
| `placements`, `portals` | Existing flat life/portal fields on maps |
| `raw` | Complete scalar tree of the entity definition node, excluding Canvas and Sound payloads |

Nested dictionary indices remain strings. Scalars retain their JSON type and tuples become arrays. UOL is retained as `{"$type":"UOL","value": ORIGINAL_VALUE}` without following it. Unsupported non-JSON values fail with their address. An empty Property node becomes `{}`, not a fabricated record. Absent fields remain absent. No missing numeric value becomes zero. Filter projection accepts only a non-boolean integer or signed integer string within SQLite's signed 64-bit range. Other values remain raw and project to NULL. There is no `incWAT`/`incPAD` alias and no skillType, reqJob, or job_GL interpretation.

Evidence retains `role`, `resource`, `image`, `node`, `source_id`, `file_sha256`, and `image_sha256`. Resource paths are relative to Data. Image addresses and node paths are original source locations. Grouped item nodes, `skill/<raw_id>`, quest sections, set entries, and recipe paths identify the actual definition. Hashes refer to resource bytes and decoded image bytes, not reserialized JSON.

Identical duplicates merge evidence. Conflicting static definitions or conflicting names fail with both addresses. Name-only entities remain visible with coverage gaps. Static-only entities have a null name. Eqp String names use their family path when known. Otherwise a unique matching static equipment/item ID determines kind. An unresolved Eqp-only name uses equipment with `name_family_unresolved`. The assembler must not duplicate one static object as a name-only item of another kind.

## Shared helper and domain interfaces

`scripts/cw_common.py` owns archive helpers and plain-dictionary contracts. `cw_source.py` owns file selection, names, fingerprints, assembly, and final source verification. Domain modules return data and never open SQLite or write files.

The common API is:

```python
context = {
    "data_root": Path,
    "files": list[Path],
    "file_hashes": dict[str, str],  # Relative resource path to SHA256.
    "names": dict[tuple[str, str], dict],
}

def iter_images(context, paths, accept):
    # accept(relative_resource: str, internal_image: str) -> bool
    yield {
        "resource": str,
        "image": str,
        "tree": dict,
        "file_sha256": str,
        "image_sha256": str,
    }

def evidence(image, *, role, node, source_id) -> dict: ...
def normalize_id(raw_id: str, kind: str) -> str: ...
def flatten(value, prefix: str = "") -> dict[str, object]: ...
def integer_or_none(value) -> int | None: ...
def entity(kind, raw_id, fields, sources, name=None) -> dict: ...
```

`iter_images` takes only prepared WZ/MS paths, sorts paths and internal addresses, normalizes backslashes, and applies its predicate before parsing payloads. WZ retains PKG1 validation and the BMS read-only key. MS retains MsFileV2 and BMS WzImage. Parse warnings and truncation fail with an address. Valid empty trees are yielded. Domain logic, not the generic helper, determines whether an empty entity definition is meaningful. Iteration closes archives on completion or failure.

`entity` normalizes identity without mutating shared state or attaching String names. The name index is read-only. Its values retain the existing `original_id`, `name`, `name_info`, and `sources` shape. The assembler attaches final names once and validates duplicates. QuestInfo supplies quest names directly.

Every domain exports `extract(context) -> DomainResult` with all these keys:

```python
{
    "entities": list[dict],
    "relations": list[dict],
    "placements": list[dict],
    "coverage": dict,
    "gaps": list[dict],
}
```

Unused lists are empty. Coverage is keyed by owned kind and records selected-file, parsed-image, and static-record counts. The assembler computes final entity/name counts. It retains all extended kinds even at zero records.

| Module | Owned extraction |
| --- | --- |
| `cw_extract_actors.py` | Existing mobs/maps, NPCs, placements, and portal references |
| `cw_extract_items.py` | Character/Item families, sets, and recipes |
| `cw_extract_skills.py` | Skill MS definitions, book/group data, and prerequisites |
| `cw_extract_quests.py` | QuestData and literal typed references in Check/Act |

String extraction remains in `cw_source.py`. Supported roots are Eqp, Cash, Consume, Etc, Ins, Pet, Mob, Npc, Map, and Skill `.img`. Existing required-root checks remain. Missing new roots produce coverage gaps. Domain authors use the declared helper interface without editing common/source files. Their fixture tests can use literal image dictionaries before shared integration finishes.

## Relations

A relation is a literal static reference, not proof of live availability, crafting success, quest accessibility, or drop probability.

```python
{
    "source_kind": str,
    "source_id": str,
    "relation": str,
    "target_kind": str,
    "target_id": str,
    "node_path": str,
    "raw": dict,
    "sources": list[dict],
}
```

IDs are normalized. `node_path` distinguishes repeated references and is relative to the image. `raw` preserves the local reference subtree, including phase, signed count, state, coordinates, or prerequisite level. Every source endpoint must exist. Missing targets remain edges and appear as `resolved=false`; no placeholder entity is created.

| Source shape | Predicate and target |
| --- | --- |
| Map life node, type m or n, numeric id | `map_mob` to mob or `map_npc` to npc |
| Map portal node, numeric tm | `portal_target` to map, retaining unresolved sentinel values |
| Skill req numeric key | `skill_req` to skill; value retained as raw required level |
| Quest Check/Act stage npc | `quest_check_npc` or `quest_act_npc` to npc |
| Quest Check/Act stage item/mob/quest indexed id | Corresponding `quest_check_item`, `quest_act_item`, mob, or quest predicate |
| Quest Act stage nextQuest | `quest_next` to quest |

Item references initially use target kind `item_ref`. After assembly a unique equipment/item match replaces that marker. Zero or multiple matches retain the marker and add an unresolved/ambiguous gap. Each stage and repeat node remains separate. There is no translation of quest phase codes, state codes, prop, potentialGrade, job_GL, or negative item counts.

Current set and recipe images are empty. Their implementation enumerates raw set top-level entries and recipe group/entry nodes when present. It adds no speculative set membership, material, or output relationships. Nonempty future content remains retrievable in full raw detail; a `reference_shape_unmapped` gap states that links need source-backed mappings. Recipe entry IDs do not establish output items. Synthetic nonempty fixtures verify lossless record extraction and composite IDs, not real-client set/recipe availability.

The existing placements table remains a projection of `map_mob`, preserving its `map_id`, `node_id`, `mob_id`, `x`, `y`, and `raw_json` shape. NPCs use the general relation table. Validation compares mob relations and placements to prevent double counting. Related lookup exposes incoming and outgoing direction and never recursively expands targets.

## SQLite and schema transition

Schema 2 retains metadata, entities, placements, and entity_fts. Entities accepts all nine kinds. It adds nullable `family TEXT`, `req_level INTEGER`, and `req_job INTEGER` beside existing mob numeric columns. Equipment req columns project only `info/reqLevel` and `info/reqJob`. Indexes cover declared filters and relation endpoints.

The new relations table contains `source_kind`, `source_id`, `relation`, `target_kind`, `target_id`, `node_path`, `raw_json`, and `sources_json`. The six identity text columns form its primary key. The source endpoint has a composite foreign key to entities. Targets deliberately have no foreign key. FTS indexes source names and readable description/static/domain strings but excludes duplicate raw trees and hashes. Stored records still retain those raw trees.

Metadata preserves identity, schema/parser revisions, build time, selected file hashes, fingerprint, per-kind counts, and coverage. It adds relation totals and counts by predicate. Gap records contain kind, category, count, and bounded source-addressed examples. Categories include source absent, image absent, source present empty, name/static mismatch, unresolved targets, unclassified families, and unmapped reference shapes. Zero domain counts remain in metadata and status.

The parser revision hashes sorted relative paths and bytes for `cw_source.py`, `cw_common.py`, every `cw_extract_*.py`, `cw_rag.py`, and the vendored parser subset. A newly added/removed domain module changes that revision. Including the writer catches changed projections. Tests and docs do not change it. The source fingerprint includes schema version, parser revision, identity, and the complete selected resource/hash list.

Status/query remain read-only snapshot operations. They never inspect the client or refresh. Schema 1 stays readable for existing mob/map commands and reports `schema_version=1`, `legacy_snapshot=true`, and `rebuild_required=true`. A new kind/filter/detail option against schema 1 returns a rebuild-required error, not an empty result. Unknown schemas and wrong identities remain errors.

An explicit build replaces a valid schema 1 database by rebuilding into a separate file, without in-place migration:

1. Validate output location and any existing snapshot's identity, known schema, and integrity.
2. Prepare sources. No-change is possible only for a valid schema 2 snapshot with matching source/parser fingerprints.
3. Extract and write a staged database in the same output directory. Validate integrity, entity/FTS counts, relation ownership/counts, placement projection, and identity.
4. Rehash the selected source set after extraction and again before publication. A change fails without replacing the old catalog.
5. For a schema 1 transition, create and verify `catalog.schema1.<old-file-sha256>.sqlite` in the output directory. Reuse only an existing identical backup. Backup failure prevents replacement.
6. Close handles and atomically replace catalog.sqlite with the validated staged file using `os.replace`.

The backup copies only the generated database. Client resources are never copied or changed. Owned temporary files are removed on failure. Pre-replacement failures preserve the old catalog. A replacement error leaves the old catalog and backup intact. A later readback failure is reported as uncertain publication/readback with its backup path, not as successful rollback. Unknown schemas, damaged old catalogs, and wrong identities require a new output directory rather than overwrite.

## CLI and output limits

Existing inspect/build/status/query commands remain. Inspect inventories files without parsing payloads. The existing query flags remain, with these additions:

| Option | Contract |
| --- | --- |
| `--kind` | One of the nine entity kinds |
| `--family NAME` | Exact source family, equipment or item only |
| `--min-req-level N`, `--max-req-level N` | Inclusive reqLevel range, equipment only |
| `--req-job-raw INTEGER` | Exact raw reqJob equality, equipment only, including negative values |
| `--skill-level N` | Positive exact level, requires kind skill and exact ID |
| `--detail` | Paginated scalar leaves, requires kind and exact ID |
| `--field-prefix PATH` | Literal detail-path prefix; detail only |
| `--field PATH` | One exact detail path; incompatible with field-prefix |
| `--value-offset N` | String offset for an exact field, default zero |
| `--offset N` | Match offset or detail-leaf offset, default zero |
| `--related-offset N` | Offset applied separately to incoming and outgoing edge pages; requires related, default zero |

ID and text remain mutually exclusive. Monster level/HP flags remain monster-only. Reversed ranges fail. NULL values do not match numeric filters. ID normalization uses the shared helper, including compound recipe IDs. Raw reqJob equality does not answer which jobs can equip an object.

Skill-level lookup reports `explicit_level_present`. It selects stored `skill_static.level[N]`, preserves mixed value types, and resolves a matching name_info description using the level's raw hs key when that key exists in name_info, otherwise hN when present. It reports which key was used. It never interpolates absent levels or evaluates common expressions. An absent level differs from an absent skill. Common data remains retrievable through detail, with its available prefix stated.

Normal results retain status, snapshot_only, snapshot, filters, total, returned, truncated, and matches. A match returns identity, name, typed numeric projections, family, and bounded source summaries. Browse, text, and exact-ID results omit raw nested trees and expose `detail_available=true`. Schema 2 map IDs use explicit detail for life/portal data. Schema 1 retains its existing exact-map behavior.

Limit stays 1 through 50, default 10. It caps matches in normal mode and scalar leaves in detail mode. Detail contains `field_total`, `field_returned`, `field_offset`, `field_truncated`, and `field_next_offset`. Paths sort lexically, use `fields/...` or `sources/...`, and flatten array indices. Null remains a leaf. Prefix matching is literal. A selected skill level restricts detail to that level and its available source description. The level response itself uses the same bounded leaf representation, so it cannot bypass detail limits.

Every emitted source string is capped at 4096 characters. Truncated field leaves report original length, offset, and next value offset. Exact field plus value-offset retrieves the rest. A value-offset without an exact string field fails. Source summaries include at most three evidence objects, source_count, and sources_truncated. Complete sources remain available through paginated detail. Related edges include at most one evidence object and a compact raw scalar summary, never a full subtree.

Related returns at most limit incoming and limit outgoing edges per match, with endpoint identity/name, predicate, direction, and resolved state. Each direction reports total, offset, next offset, and truncation. Edge order is stable by predicate, other endpoint kind/ID, and node path. Existing maps/mobs placement summaries remain for those kinds and use the same related offset. These are static placement counts, not live server populations.

Text input is limited to 512 characters and 32 quoted tokens. Paths are limited to 1024 characters. SQL uses bound parameters. Punctuation cannot become an FTS operator. Expected argument/source/database errors remain JSON with exit code 2. Related raw summaries contain at most 16 scalar leaves. Snapshot summaries contain at most 50 gap groups with at most three examples each, plus omitted counts. The finite entity/edge/leaf/source/string limits bound output size.

## Execution briefs and validation

These briefs become accepted execution only after the Primary accepts this contract. Each owner edits only its assigned files and returns changed paths, named checks, and unresolved assumptions.

| Owner | Files and concrete work | Required targeted evidence |
| --- | --- | --- |
| Shared source | `cw_common.py`, `cw_source.py`, `tests/test_cw_source.py`. Implement helpers, selected paths, names, assembly, item-ref resolution, fingerprints, and coverage. | Marker rejection, source mutation, duplicate conflict, valid empty image, new-module fingerprint change |
| Actors | `cw_extract_actors.py`, `tests/test_cw_actors.py`. Move existing mobs/maps, add NPCs and guarded life/portal relations. | Same mob placements, distinct NPC type, unresolved portal, raw NPC speak |
| Items | `cw_extract_items.py`, `tests/test_cw_items.py`. Read grouped Item and individual Character/Pet, all info/spec scalars, raw sets/recipes, composite recipe keys. | incWAT 17/27 preserved, Consume hp 100/250/500, reqJob raw, empty set/recipe coverage, synthetic same recipe entry in two groups |
| Skills | `cw_extract_skills.py`, `tests/test_cw_skills.py`. Read skill nodes, explicit levels/req, book and group data. | Mixed numeric types preserved, exact levels and hs, source-backed prerequisite; synthetic common text retained without evaluation |
| Quests | `cw_extract_quests.py`, `tests/test_cw_quests.py`. Preserve four subtrees and guarded source references. | Distinct stages, signed count/state/job_GL unchanged, npc/item/mob/quest/nextQuest edges, unresolved target |
| Retrieval | `cw_rag.py`, `tests/test_cw_rag.py`, `tests/test_cw_schema2.py`. Implement schema/projections/FTS/relations, bounded CLI, and schema 1 read/rebuild. | Safe punctuation, NULL exclusion, raw equipment filters, skill level, detail/string continuation, unresolved edges, successful legacy rebuild, failed rebuild preserving old hash |
| Skill/docs | `SKILL.md`, `README.md`, `AGENTS.md`. Document accepted CLI and scope, original English, snapshot behavior, raw-code limitations, user-triggered refresh. | Examples agree with --help and real result shape; remove the superseded monster/map-only scope under current authorization |

Domain owners do not edit shared source or SQLite files. The shared-source owner integrates imports after modules are available. Retrieval can proceed concurrently against literal snapshot fixtures. Docs can proceed against the accepted CLI contract. No owner edits this design while another relies on it without returning the change to the Primary.

The final acceptance owner uses the real client only after source/retrieval owners release their files. It records selected source hashes, domain counts, gaps, and source-addressed samples. Static records, not name-only matches, prove extraction. It checks equipment requirements/raw job, item effects, explicit skill levels/prerequisites, NPC maps, quest fields/links, empty set/recipe sources, and retained mob/map behavior. Nonempty set/recipe support is fixture-verified and is not advertised as observed real-client content.

The acceptance owner runs the authorized full cw-rag test suite after integration, preserves its result, then runs the real local build/query checks. Repeating build must return no_change without altering catalog bytes or mtime. Legacy transition/recovery uses a disposable generated fixture or copy, never intentional corruption of the actual client/catalog. During that original acceptance phase, packaging, release, commit, push, installation, and deployment were outside the authorized scope. The subsequent delivery authorization is recorded below.

## Current Goal checkpoint

<!-- cw-rag-current:start -->
Goal: finish the user's four requested outcomes for standalone CW retrieval: full static extraction, SQLite retrieval and relations, Skill/docs, and real-client acceptance. All four outcomes are complete and the original Goal is closed. The user subsequently requested Codex directory-link installation, GitHub publication, repository GEO/SEO improvements, a Chinese default README, and player-guide illustrations using original client character sprites without redrawing them.

Acceptance: source-backed equipment, items, skills, NPCs, quests, and retained mobs/maps; source-addressed empty sets/recipes with synthetic nonempty extraction checks; original English, IDs, and hashes; bounded detail/search; explicit raw-code semantics; visible unresolved relations; schema 1 readable recovery and explicit schema 2 replacement; authorized full cw-rag suite and real-client/no-change evidence. All acceptance criteria are met.

Permissions: the user authorized full implementation, parallel work, an explicit Goal, the local client build, and the full cw-rag test suite. On 2026-10-10, the user also authorized Codex directory-link installation, commits and pushes to the existing public GitHub repository, repository GEO/SEO documentation and metadata improvements, and illustration generation and integration. Packaging, releases, website deployment, and client writes remain outside this follow-up scope. User Stop takes precedence. No Stop is recorded.

Owners: /root owns final acceptance and this checkpoint. The original implementation and real-client acceptance owners have finished and released their files and generated index. Installation, documentation, and GitHub operations use separate resource owners under the follow-up authorization.

State: schema 2 extraction, SQLite retrieval and relations, Skill/docs, integration, and real-client acceptance are complete. The full suite passed 59 tests. The accepted snapshot is `.cw-rag/acceptance/catalog.sqlite`, built from `J:/nexon/maplestorycw/appdata/Data`; it contains equipment 4,090, item 3,565, skill 106, NPC 226, quest 316, mob 101, map 319, set 0, and recipe 0. Static definition counts are equipment 4,083, item 3,554, skill 106, NPC 226, quest 316, mob 98, and map 305. QuestInfo supplies all 316 quest names with no missing-name gap; the coverage correction did not change entity or relation totals. The snapshot has 12,884 relations and 2,107 unresolved targets. Repeated build returned `no_change` and preserved the catalog bytes, modification time, `built_at`, and schema 1 backup SHA256 `f1ce84ab05dc1889ca2f77462b553c83dcd370ea303e6ca49c3091cfabaf0a23`. Snapshot metadata: `built_at` `2026-10-09T21:06:39Z`, parser revision `cc450857aecfe934e26e05542aa5dcf73cc13c4b7a60a440e2631df8ffc6480e`, source revision `a49e9d09aae23c249d3d52438f83d7a70d0bb4f279838ddd4a63f4460efa902f`. The checks verify local source and CLI behavior; no external AI call or Skill installation was performed.

Delivery follow-up acceptance: the Codex Junction targets `J:/PigeonYang/Skills/cw-rag`; linked and source `SKILL.md` hashes match; the installed CLI reads the schema 2 snapshot. GitHub delivery is checked through matching local and remote `main` commits, published README contents, description, and Topics. No new implementation, suite, or client build is required.

Installation evidence: `J:/Users/yangda01/.codex/skills/cw-rag` was created as a Junction on 2026-10-10. Its `SKILL.md` SHA256 matches the source at `33f53c9125b843dc4a17da6daa9ab65152807d608be11e8efaaa6c4d6279d3bc`. The installed CLI `--help` and snapshot `status` exited 0, with `status=available`, `schema_version=2`, and `snapshot_only=true`. This proves the link and CLI access; a new Codex session's Skill invocation has not been tested. The original acceptance run described above did not install the Skill.

Illustration evidence: four 1672 by 941 PNGs are in `assets/` and referenced by the Chinese default README. The user rejected generated character approximations, so original client Manji, Roger, and Sera frames were inserted with integer nearest-neighbor scaling. A confirmed equipment-poster placement issue was corrected by aligning the foreground character with the floor and occluding the shopkeeper's lower body at the counter. Visible opaque character pixels match the original frames; the final equipment check compared 51,408 sprite pixels with zero mismatches. Local composition evidence is `.cw-rag/illustrations/composition-evidence.json`; public asset provenance and final background prompts are in `docs/ILLUSTRATIONS.md`. /root visually reviewed all four final images.

Evidence: `.cw-rag/full-acceptance/coverage-fix-summary.json`, `.cw-rag/full-acceptance/coverage-fix-unittest-full.txt`, `.cw-rag/full-acceptance/acceptance-summary.json`, `.cw-rag/acceptance/catalog.sqlite`, `.cw-rag/acceptance/catalog.schema1.f1ce84ab05dc1889ca2f77462b553c83dcd370ea303e6ca49c3091cfabaf0a23.sqlite`, and `.cw-rag/full-acceptance/coverage-fix-build-repeat.json`; parser sources and hashes are in `docs/SOURCE_PROVENANCE.md`.

Known source gaps: `SetItemInfo.img` and `ItemMake.img` are valid but empty, reported as `source_present_empty`; synthetic fixtures cover future nonempty extraction but are not current client records. Of the 2,107 unresolved targets, 2,104 portal edges point to the literal target `999999999`, one each points to missing maps `10006152` and `10006120`, and one is a quest reference. No meaning is assigned to `999999999`. No verified interpretation exists for `reqJob` masks, `skillType`, `job_GL`, quest stage/state codes, or undocumented expressions.

Checkpoint route: this supplied current region has no task-context binding metadata. Preserve exactly one marker pair. The pstack task-context helper remains available for bound records. The parent owns the continuation assessment and receives this native child handoff. No scheduler or automatic wakeup is installed.
<!-- cw-rag-current:end -->
