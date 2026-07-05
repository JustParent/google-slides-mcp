---
name: google-slides
description: >-
  Build and edit Google Slides presentations with the google-slides-mcp server.
  Use when the user wants to find existing decks in their Drive (Google Slides or
  .pptx), assemble a deck by reusing styled example slides from a
  showcase/template, fill or restyle slide content, move/resize elements
  (bounding boxes), reorder or hide slides, or render slides to PNG to verify the
  result looks right. Covers the high-fidelity "copy a showcase, duplicate the
  slides you want, fill, prune" workflow.
---

# Google Slides (low-level MCP)

This skill explains the canonical way to drive the **google-slides-mcp** server.
Tools are exposed as `mcp__google-slides__<tool>` (shown below by bare name).

## Mental model: copy → reuse → fill → prune

The Slides API has **no cross-presentation slide copy**, and recreating elements
by hand loses styling fidelity. So the high-fidelity pattern is always:

1. **Copy the whole showcase deck** (`copy_presentation`) — this clones masters,
   layouts, theme and color scheme, and brings *every* example slide along as a
   reusable **palette**.
2. **Duplicate the example slides you want** (`duplicate_slide`) *within* that copy.
3. **Fill** them with real content (`set_element_text`, `replace_all_text`).
4. **Prune** the leftover palette slides at the very end (`prune_parked_slides`).

Editing the copy this way is functionally identical to hand-editing the showcase.

> **Never** rebuild a slide by reading its elements and recreating them in a
> different deck — that drops theme/master/placeholder styling and will not be
> pixel-perfect. Always start from `copy_presentation`.

## Prerequisites

- The MCP server is configured in the client, and the user has run the one-time
  login (`google-slides-mcp-auth`). If a tool returns "No cached Google
  credentials…", tell the user to run that command; do not try to work around it.
- You need the **showcase/template presentation ID** (the long ID in its URL:
  `https://docs.google.com/presentation/d/<ID>/edit`). If the user doesn't give
  you one, find it with `search_presentations` (see below) before asking.

## Finding existing decks (Drive discovery)

`search_presentations` searches the user's Drive across **native Google Slides
and PowerPoint (`.pptx`/`.ppt`) files** — use it whenever the user refers to a
deck by description rather than ID:

```
search_presentations(order_by="createdTime desc", owned_by_me=true, max_results=1)
  → "the last presentation I created"
search_presentations(name_contains="corporate template", file_type="google_slides")
  → find a template by name
search_presentations(full_text_contains="Q3 revenue")
  → decks mentioning a topic (order_by is ignored by Drive for full-text queries)
```

Results include `fileType` and `directlyEditable`. Only native Google Slides
files can be used as a `presentation_id`; for a `.pptx` result, convert it first
(the original file is left untouched):

```
import_presentation(file_id="<pptx_id>")   → { presentationId, url }
```

So "take my last deck and port it into our corporate template" is:
`search_presentations` (find source, import if `.pptx`) → `search_presentations`
(find template) → `copy_presentation` (template) → `catalog_slides` on both →
rebuild each source slide on a duplicated template layout.

## Canonical workflow

### 1. Start a project (once)

```
copy_presentation(source_id="<showcase_id>", title="Q3 Review")
  → { presentationId: "<deck>", url: ... }      # the working deck
catalog_slides(presentation_id="<deck>")
  → [ { index, objectId, layoutObjectId, elementTypes, placeholders, title, isSkipped } ]
park_slides(presentation_id="<deck>", slide_ids=[<every example objectId>])
  → hides the palette so it doesn't clutter the in-progress deck
```

`catalog_slides` is how you choose which example slide fits each section (look at
`placeholders`, `elementTypes`, and `title`). Keep the catalog — you'll reuse the
example `objectId`s on later turns.

### 2. Add a slide (repeat, across as many turns as you like)

You do **not** have to do everything at once. Each call is independent, so "now
add another three-column slide that says X" is just another pass:

```
duplicate_slide(presentation_id="<deck>", page_id="<3col_example_id>", insertion_index=2)
  → { newSlideId: "<slide>" }
get_page(presentation_id="<deck>", page_id="<slide>")
  → find the element objectIds you need to fill
set_element_text(presentation_id="<deck>", element_id="<col1>", text="…")
set_element_text(presentation_id="<deck>", element_id="<col2>", text="…")
```

Filling strategy:
- If the showcase uses **placeholder tokens** like `{{title}}`, prefer
  `replace_all_text(presentation_id, mappings={"{{title}}": "Results"},
  page_ids=["<slide>"])` — scope it to the new slide so you don't rewrite the
  whole deck.
- Otherwise use `set_element_text` per element (replaces all of that element's
  text in one call; safe on empty elements).

### 3. Adjust layout (optional)

- Move/resize an element by its bounding box in **points**:
  `set_element_box(presentation_id, element_id, x_pt, y_pt, width_pt?, height_pt?)`.
- Reorder slides: `reorder_slides(presentation_id, slide_ids, insertion_index)`.
- Z-order / grouping: `set_z_order`, `group_elements`, `ungroup_elements`.
- For anything not covered by a convenience tool, use the raw escape hatch
  `batch_update(presentation_id, requests=[...])` (see below).

### 4. Verify

```
render_page(presentation_id="<deck>", page_id="<slide>", size="MEDIUM")   → PNG to inspect
diff_pages(presentation_id="<deck>", page_a="<slide>", page_b="<example_id>")
  → mismatch ratio (0.0 = pixel-perfect) + a visual diff image
```

Render after meaningful edits and confirm the slide looks correct before moving on.

### 5. Finish (once, at the end)

```
prune_parked_slides(presentation_id="<deck>")   → deletes ALL parked slides
```

This removes the hidden palette. **Always do this last.** If you need a layout you
never instantiated, do it *before* pruning — once pruned, that example is gone
(no cross-deck copy to bring it back).

## Raw `batch_update`

`batch_update` takes a list of raw Slides API request objects and applies them
atomically (one write quota unit no matter how many sub-requests). Use it for
operations without a dedicated tool, e.g. styling:

```
batch_update(presentation_id="<deck>", requests=[
  { "updateTextStyle": {
      "objectId": "<el>", "textRange": {"type": "ALL"},
      "style": {"bold": true, "foregroundColor": {"opaqueColor": {"themeColor": "ACCENT1"}}},
      "fields": "bold,foregroundColor" } }
])
```

`updateShapeProperties`, `updateTextStyle`, and `updateSlideProperties` **require a
`fields` mask** — the tool warns if it's missing. Reference for request shapes:
https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/request

## Cost discipline (keep it finite)

Every tool makes a bounded number of API calls and returns trimmed output. To keep
a session cheap and predictable:

- Use `list_slides` / `catalog_slides` to find IDs; only `get_page(..., raw=True)`
  or `get_presentation(..., raw=True)` when you genuinely need full detail.
- Batch many edits into one `batch_update` rather than many small calls.
- `render_page` / `diff_pages` hit the **expensive** thumbnail quota (300/min per
  project, 60/min per user). Render specific pages you changed — never loop over a
  whole deck. Prefer `MEDIUM` size unless you need fine detail.

## Troubleshooting

- **"No cached Google credentials…"** → user must run `google-slides-mcp-auth`.
- **`insufficient scopes` / can't copy a template** → the OAuth grant needs the
  `drive` scope; user should re-run auth after enabling it on the consent screen.
- **Token stopped working after ~7 days** → the OAuth app is in *Testing* status;
  re-run the auth command (or publish the consent screen).
- **A duplicated slide looks wrong** → confirm you duplicated from the showcase
  example (full fidelity), not rebuilt it from scratch; render and `diff_pages`
  against the original example to see the delta.
