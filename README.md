# ArcBreakout

Export Arc browser spaces as individual bookmark HTML files that can be imported
into Chrome, Firefox, Safari, or Edge. Each space becomes one
`NETSCAPE-Bookmark-file-1` file, with Arc's pinned folders preserved.

## Requirements

- macOS with Arc installed
- Python 3.7 or newer (standard library only)

## Usage

```bash
./arc_bookmarks_export.py
```

By default this reads:

```
~/Library/Application Support/Arc/StorableSidebar.json
```

and writes one HTML file per space to `./arc-bookmarks`, exporting only pinned
bookmarks.

### Options

| Option | Description |
|---|---|
| `-i, --input PATH` | Path to `StorableSidebar.json` |
| `-o, --output-dir DIR` | Output directory (default: `./arc-bookmarks`) |
| `--scope {pinned,all}` | `pinned` exports the pinned section only; `all` also includes unpinned/open tabs (default: `pinned`) |
| `--space NAME` | Export only this space; repeat for multiple spaces. Case-insensitive exact match. |
| `-h, --help` | Show usage |

### Examples

Export every space's pinned bookmarks:

```bash
./arc_bookmarks_export.py
```

Include unpinned/open tabs as well:

```bash
./arc_bookmarks_export.py --scope all
```

Export selected spaces to the Desktop:

```bash
./arc_bookmarks_export.py --space Work --space Research -o ~/Desktop/arc-bookmarks
```

Export from a copy of Arc's data file:

```bash
./arc_bookmarks_export.py -i ./StorableSidebar.json -o ./out
```

## Output

Each Arc space produces `<Space>.html`. The file wraps the space's contents in a
folder named after the space, so importing several files keeps the spaces
separate. Duplicate space names are disambiguated as `Slay` and `Slay (2)`.

Within a space:

- Arc folders are preserved, including nested folders.
- Split views are flattened into their individual tabs.
- Bookmark titles use Arc's custom title when present, then the saved page title.
- `ADD_DATE` values are converted from Arc's Apple-reference timestamps to Unix
  seconds.
- Titles and URLs are HTML-escaped.
- Unpinned/open tabs are included only with `--scope all`.
- Profile-level Favorites are not included because they are not scoped to a
  single space.
- Arc-internal URLs such as `arc://` and `chrome-extension://` are exported
  as-is.
- Spaces with zero bookmarks still produce a valid, empty bookmark file.

## Notes

Arc should normally be closed, or at least idle, while exporting. If the input
file is being rewritten, JSON parsing fails with an error and you can retry.

The script only reads Arc's data; it never modifies `StorableSidebar.json`.
