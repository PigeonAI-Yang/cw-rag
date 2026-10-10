# Parser source provenance

The standalone adapter in `scripts/cw_source.py` is derived from the verified read-only CW extraction path in the local Maple Atlas source. The adapter has no runtime import or hard-coded reference to that project.

- `J:\PigeonYang\sites\maple-atlas\scripts\classic_world.py` SHA256: `4b6af460154773ea609d6f961857e8e1ad4071333972d52464328e8e2d17e58c`. Reused behavior includes scalar-tree extraction that skips Canvas, Sound, and UOL nodes; `WzFile.open(..., region="BMS", writable=False)` for PKG1 archives; image parse completeness checks; raw image-byte hashing; and MS entry decoding through `MsFileV2` and `WzImage.from_bytes`.
- `J:\PigeonYang\sites\maple-atlas\scripts\static_metadata.py` SHA256: `edbaf587e1775c3f054da8ffe9f17ff27274ab043569d2c14c49657272415c92`. Reused map info field names and life/portal scalar field paths match the existing extraction contract.

The maintained local parser and retrieval modules have these SHA256 hashes:

| File | SHA256 |
| --- | --- |
| `scripts/cw_common.py` | `d3af621554fdd2f40f472358fa6b79dff5b74663ed0f595e3357dee0096a71b6` |
| `scripts/cw_extract_actors.py` | `99e52a0c5787f27dc8a18c5bc09d23cc93b38ad7698cee36245ae3655d8d74b8` |
| `scripts/cw_extract_items.py` | `356a899e3853da4a532b43facd4231b4933016631b50043ea7450b2746964435` |
| `scripts/cw_extract_quests.py` | `accb84dcc8dd1e8827501de0f14babc6f2308dee0d0600f0f935b2ce481ab502` |
| `scripts/cw_extract_skills.py` | `dfac5562d5086bcadded2ba110730478a9a6c699e025dc60682e706744bffd69` |
| `scripts/cw_rag.py` | `40ff13a3707dbf87a8e0cea662e35b7d7851ba05253bc820bdbf90196457af06` |
| `scripts/cw_source.py` | `cc0fb0e062ea2a64bffd7495bf8767507107214253d97db3e8b92b20ef301e9d` |

`parser_revision` is calculated from the relative paths and current bytes of `cw_common.py`, `cw_rag.py`, `cw_source.py`, every `cw_extract_*.py` module, and the selected vendored parser files. It is not a pinned hash of `cw_source.py` alone. The accepted snapshot recorded parser revision `cc450857aecfe934e26e05542aa5dcf73cc13c4b7a60a440e2631df8ffc6480e`; its source revision fingerprints the selected client resources together with the schema identity and parser revision. Recompute these values after source or parser changes.

The read-only parser subset comes from [Leonana69/wz-python](https://github.com/Leonana69/wz-python), commit `2cfc443e0e92b4476664a256ab1e1ae6aa229cd0`, under MIT. The upstream `LICENSE` and `provenance.json` are copied unchanged in `scripts/vendor/wzpy/`. This project includes only `__init__.py`, `crypto.py`, `reader.py`, `properties.py`, `wz_file.py`, `wz_image.py`, `chacha20.py`, and `ms_file_v2.py`; it excludes Canvas decoding, Pillow, image writers, and unrelated importer code.

| Vendored file | SHA256 |
| --- | --- |
| `__init__.py` | `9e4d1b435bf4e692d5e486e5a835b9e9cae559707f6dcb82ade9d86f52371380` |
| `crypto.py` | `e41993e59a4ee33ff3849ec8f5678ca28f08d88029e1f857c63d28ff4454da01` |
| `reader.py` | `2549a31ba401a3e7e0588a14a40e8a02a3c1dd0c1b2f840a38242f187987cf98` |
| `properties.py` | `68499ab81fa1321343ff1cfa2c179af32c6c14c11ca811bbe1b762c93121b28a` |
| `wz_file.py` | `54dcf8c2ff900984e34dc8db2e62650dd761f62f0d053836aed00857ff47f8eb` |
| `wz_image.py` | `239bd74f94b0a6dff2e264b746390bf4965d17280a346e52fa7cf074f68b7af5` |
| `chacha20.py` | `d79913b1c342875335726bfbfbf59163957b625c36d9c06d852a9d74c9cc2d09` |
| `ms_file_v2.py` | `c4c72dc0d7b9eb3c1d653d75383e02961010b030cff54b85bd59f25a85f79bfe` |

The only external runtime dependency is `pycryptodome==3.23.0`, declared in `requirements.txt`. SQLite FTS5 and all other runtime modules are from Python's standard distribution.
