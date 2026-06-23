# google-slides-mcp

A `uvx`-runnable [MCP](https://modelcontextprotocol.io) server for **low-level**
interaction with the Google Slides API.

It is built to be more capable than typical Slides MCPs. In addition to reading
presentations and slides as structured objects, it can:

- **Reuse a "model template" / showcase deck with full fidelity.** Copy a styled
  showcase deck, duplicate the example slides you want (e.g. a 3‑column layout),
  fill them with content, and prune the rest — leaving a result *functionally
  identical to hand‑editing the showcase* (masters, layouts, theme and colors are
  all preserved).
- **Edit element bounding boxes** in points (position/size) without doing affine
  matrix math, plus raw transform / z‑order / grouping control.
- **Render slides to PNG** and **diff two renders** so you can verify output is
  pixel‑perfect.
- **Issue raw `batchUpdate` requests** for anything the convenience tools don't
  cover.

Every tool makes a **bounded number of API calls** and returns **trimmed output**,
so an LLM driving it has a predictable, finite cost.

---

## Why the template workflow works this way

The Google Slides API has **no native cross‑presentation slide copy**.
`duplicateObject` only works *within* one presentation, and recreating a slide's
elements by hand in another deck loses fidelity (placeholder inheritance, master
styling and theme colors are dropped).

The high‑fidelity pattern this server implements is:

1. **`copy_presentation`** → Drive `files.copy` clones the *entire* showcase deck,
   preserving masters, layouts, theme and color scheme.
2. **`catalog_slides`** → summarize the example slides so you can pick one per section.
3. **`duplicate_slide`** → copy a chosen example slide *within* the new deck (full fidelity).
4. **`replace_all_text`** / **`set_element_box`** → fill content and adjust layout.
5. **`delete_objects`** → prune the original showcase example slides.
6. **`render_page`** / **`diff_pages`** → verify the result.

---

## 1. Google Cloud Console setup (one time)

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and **create
   a project** (or select an existing one).
2. **Enable the APIs**: *APIs & Services → Library* → enable both
   **Google Slides API** and **Google Drive API**.
3. **Configure the OAuth consent screen**: *APIs & Services → OAuth consent screen*.
   - User type: **External** (or **Internal** if you're in a Workspace org).
   - Fill in app name / support email.
   - Add the scopes `.../auth/presentations` and `.../auth/drive`.
   - Under **Test users**, add the Google account you'll authorize with.
   - ⚠️ While the app is in **Testing** status, refresh tokens expire after **7
     days**. Re-run the auth command when that happens, or publish the app.
4. **Create credentials**: *APIs & Services → Credentials → Create credentials →
   OAuth client ID*.
   - Application type: **Desktop app**.
   - Download the JSON — this is your **`client_secret.json`**.

> Scope note: this server requests the broad `auth/drive` scope so it can
> `files.copy` *any* template you own by ID. If you only ever copy decks the app
> itself created, you can narrow this via `GOOGLE_SLIDES_SCOPES` (see below), but
> the showcase workflow on existing decks needs `auth/drive`.

## 2. Install & first‑time login

Authorize once with the dedicated login command (it opens a browser, then caches a
token so the MCP server itself never needs an interactive flow):

```bash
GOOGLE_CLIENT_SECRET=/absolute/path/to/client_secret.json \
  uvx --from git+https://github.com/justparent/google-slides-mcp google-slides-mcp-auth
```

This writes a token to `~/.config/google-slides-mcp/token.json` (override with
`GOOGLE_TOKEN_PATH`). Once published to PyPI you'll be able to drop `--from …` and
just run `uvx google-slides-mcp-auth`.

## 3. Configure your MCP client

Add the server to Claude Desktop / Claude Code (`mcpServers` config):

```json
{
  "mcpServers": {
    "google-slides": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/justparent/google-slides-mcp",
        "google-slides-mcp"
      ],
      "env": {
        "GOOGLE_CLIENT_SECRET": "/absolute/path/to/client_secret.json",
        "GOOGLE_TOKEN_PATH": "~/.config/google-slides-mcp/token.json"
      }
    }
  }
}
```

After PyPI publication: `"args": ["google-slides-mcp"]`.

### Environment variables

| Variable                | Default                                   | Purpose                                   |
| ----------------------- | ----------------------------------------- | ----------------------------------------- |
| `GOOGLE_CLIENT_SECRET`  | `client_secret.json`                      | Path to the Desktop OAuth client JSON.    |
| `GOOGLE_TOKEN_PATH`     | `~/.config/google-slides-mcp/token.json`  | Where the cached token is stored.         |
| `GOOGLE_SLIDES_SCOPES`  | `presentations,drive`                     | Comma‑separated scope override (advanced).|

---

## Tools

**Core read**

| Tool | Purpose | API calls |
| ---- | ------- | --------- |
| `create_presentation(title)` | Create an empty deck. | 1 |
| `get_presentation(presentation_id, include_masters?, raw?)` | Bounded structured overview of a deck. | 1 |
| `get_page(presentation_id, page_id, raw?)` | Bounded summary of one slide. | 1 |
| `list_slides(presentation_id)` | Minimal index/id/layout list. | 1 |

**Raw write**

| Tool | Purpose | API calls |
| ---- | ------- | --------- |
| `batch_update(presentation_id, requests)` | Apply raw Slides `batchUpdate` requests atomically. | 1 |

**Element / transform / bounding box**

| Tool | Purpose | API calls |
| ---- | ------- | --------- |
| `set_element_box(presentation_id, element_id, x_pt, y_pt, width_pt?, height_pt?)` | Place/size a box in points. | ≤2 |
| `update_transform(presentation_id, element_id, transform, mode?)` | Apply a raw AffineTransform. | 1 |
| `set_z_order(presentation_id, element_ids, operation)` | Reorder front/back stacking. | 1 |
| `group_elements` / `ungroup_elements` | Group/ungroup elements. | 1 |

**Text**

| Tool | Purpose | API calls |
| ---- | ------- | --------- |
| `replace_all_text(presentation_id, mappings, page_ids?, match_case?)` | Fill placeholder text. | 1 |
| `insert_text(presentation_id, element_id, text, index?)` | Insert text into a shape/cell. | 1 |

**Template / showcase reuse**

| Tool | Purpose | API calls |
| ---- | ------- | --------- |
| `copy_presentation(source_id, title, parent_folder_id?)` | Clone a deck preserving full styling. | 1 |
| `catalog_slides(presentation_id)` | Per‑slide descriptor to choose examples. | 1 |
| `duplicate_slide(presentation_id, page_id, insertion_index?)` | Full‑fidelity in‑deck slide copy. | 1 |
| `delete_objects(presentation_id, object_ids)` | Delete slides/elements (prune). | 1 |
| `reorder_slides(presentation_id, slide_ids, insertion_index)` | Move slides. | 1 |

**Rendering / verification**

| Tool | Purpose | API calls |
| ---- | ------- | --------- |
| `render_page(presentation_id, page_id, size?)` | Render a slide to PNG. | 1 (expensive) |
| `diff_pages(presentation_id, page_a, page_b, size?)` | Render two slides + pixel diff. | 2 (expensive) |

> `render_page`/`diff_pages` use the thumbnail endpoint, an *expensive* quota
> operation (300/min per project, 60/min per user). Rendering is per‑page by
> design — there is no whole‑deck render.

---

## Example: build a deck from a showcase template

```
copy_presentation(source_id="<showcase_id>", title="Q3 Review")      → new deck id
catalog_slides(presentation_id="<new_id>")                           → pick example slides
duplicate_slide(presentation_id="<new_id>", page_id="<3col_example>", insertion_index=1)
replace_all_text(presentation_id="<new_id>", mappings={"{{title}}": "Results"})
delete_objects(presentation_id="<new_id>", object_ids=["<original_examples>..."])
render_page(presentation_id="<new_id>", page_id="<new_slide>")       → verify visually
diff_pages(presentation_id="<new_id>", page_a="<new_slide>", page_b="<showcase_example>")
```

---

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest                 # unit + smoke tests (no network/credentials)
uv build                      # build sdist + wheel
```

The codebase is intentionally small and modular so it can back a Claude *skill*
that teaches the Slides API:

| Module | Responsibility |
| ------ | -------------- |
| `auth.py` | OAuth installed‑app flow, token cache, login CLI. |
| `client.py` | Builds Slides/Drive services; bounded 429/5xx backoff. |
| `units.py` | EMU/PT conversion, transform & bounding‑box math. |
| `views.py` | Trims verbose API responses into bounded summaries. |
| `ids.py` | Collision‑safe object‑ID generation. |
| `template.py` | Copy / catalog / duplicate / prune helpers. |
| `render.py` | Thumbnail rendering + Pillow image diff. |
| `server.py` | FastMCP server wiring the tools together. |

## Troubleshooting

- **"No cached Google credentials …"** — run `google-slides-mcp-auth` first.
- **Token stopped working after ~7 days** — your OAuth app is in *Testing* status;
  re-run the auth command or publish the consent screen.
- **`insufficient scopes` / can't copy a template** — ensure the `auth/drive` scope
  is granted (re-run auth after enabling it on the consent screen).

## License

MIT — see [LICENSE](LICENSE).
