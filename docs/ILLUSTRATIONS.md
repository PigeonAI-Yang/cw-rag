# Illustration assets

The four user-facing PNG assets and their topics are:

- [cw-rag-hero.png](../assets/cw-rag-hero.png): an introduction to local client-data lookup for Classic World players.
- [cw-rag-equipment.png](../assets/cw-rag-equipment.png): comparing equipment candidates and their raw attributes.
- [cw-rag-leveling.png](../assets/cw-rag-leveling.png): looking up monsters and leveling maps.
- [cw-rag-skills-quests.png](../assets/cw-rag-skills-quests.png): checking skill prerequisites and quest fields.

## Character compositing

Built-in image generation created the poster backgrounds and layouts and removed AI-drawn people. At the user's request, the final character layers were composited from original Classic World client sprites. The existing Sharp package used nearest-neighbor scaling by integer factors; the source colors and alpha were unchanged. Visible opaque character pixels were checked unchanged.

The existing local Maple Atlas public export supplied these client frames. CW RAG has no runtime dependency on the Maple Atlas website. The source sprite files are not committed separately; their character layers are present in the final illustrations. These NPCs are illustration subjects, not a player's actual avatar, screenshots, or observed gameplay results.

No Python image editor, fallback image-generation CLI, direct model API call, or new runtime dependency was added. The local composition script and evidence are under the ignored `.cw-rag/illustrations/` directory.

| Sprite | Frame size | Client source | SHA-256 |
| --- | ---: | --- | --- |
| Manji | 78 × 79 | `Npc/Npc_000.wz/0000502.img` | `9b240dca1a36eb08abb09b026d6beec5a79795ad9a7b97148cc163adb54379fe` |
| Roger | 56 × 70 | `Npc/Npc_000.wz/0000003.img` | `1d73ea447240dcd3a3512142b92d933cb88b05157f49d9314b639e6a0f614f4c` |
| Sera | 51 × 67 | `Npc/Npc_000.wz/0000002.img` | `6dadaad6abdc193ee685ca451c4343ffcd3b78cc8c838cd5f6a0646ee734d16f` |

## Final background-edit prompts

These are the final background-edit prompts, applied after the initial poster artwork. They are not the initial poster-generation prompts.

### Hero

```text
Precise background preparation edit of the CW RAG hero poster. Remove ONLY the large human character in the lower left AND the large open book he is holding, leaving clean grassy ground and forest scenery where they were. This area will receive an actual original game sprite by direct pixel compositing later. Do not draw or add any replacement person, silhouette, face, character, statue or decorative object in the removed area. Keep the laptop in place. Preserve ALL other content exactly, particularly every Chinese/English heading and caption, all parchment/wood panels, little icons, jelly monsters, colors, layout and image dimensions. Seamlessly extend the background behind the removed person/book. Do not repaint the rest. Opaque background.
```

### Equipment

```text
Precise background preparation edit of the equipment-query CW RAG poster. Remove ALL human artwork: the player below the left speech bubble, the shopkeeper behind the counter, and the little player portrait icon in the right panel's third row. Seamlessly fill their former positions with their existing shop/background/counter/parchment. Leave an empty parchment icon slot for the third row. Do NOT add replacements, silhouettes, new figures or extra objects; actual original game sprites will be pasted later. Keep the two weapons and their neutral labels, all speech bubbles, headings, three panel rows, bottom steps and footer EXACTLY intact. Preserve Chinese/English text, composition, dimensions, colors and all unrelated pixels as closely as possible. No new human or animal figures. Opaque background.
```

### Leveling

```text
Prepare this existing CW RAG monster/map poster for direct insertion of an original game sprite. Remove ONLY the human player at lower left and the small parchment map held by that player. Fill with matching clean grassy/forest background, keeping the speech bubble and its text untouched. Do not add any replacement person or creature in the freed area. Preserve the central illustrated map, pins, existing green monsters, parchment information panel, all titles/labels/Chinese and English text, bottom sentence, footer, layout and image dimensions. Inpaint just the human and held-map area. Opaque background.
```

### Skills and quests

```text
Prepare this CW RAG skills-and-quests poster for exact compositing of genuine client sprites. Remove ONLY all three humans: player and held book inside left card, player inside right card, village NPC inside right card. Inpaint the existing room/village background behind them. Leave empty ground at their former positions. Do not create replacement figures, faces, statues or silhouettes. Preserve ALL Chinese and English headings, speech bubbles, card rows, icons not depicting humans, panels, illustrative background, colors and exact image dimensions. This is an intermediate background; do not alter other content. Opaque background.
```
