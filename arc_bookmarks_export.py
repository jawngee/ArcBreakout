#!/usr/bin/env python3
"""Export Arc browser spaces as Netscape bookmark HTML files.

Arc keeps its sidebar in ~/Library/Application Support/Arc/StorableSidebar.json.
Each space has a pinned section and an unpinned section. This script walks the
ordered childrenIds of those containers, preserves Arc's folder hierarchy, and
writes one bookmark file per space. Split views are flattened into their tabs.

Usage:
    python3 arc_bookmarks_export.py                    # pinned bookmarks, all spaces
    python3 arc_bookmarks_export.py --scope all        # pinned + unpinned tabs
    python3 arc_bookmarks_export.py --space Work       # one space
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

DEFAULT_INPUT = Path("~/Library/Application Support/Arc/StorableSidebar.json")
DEFAULT_OUTPUT_DIR = Path("arc-bookmarks")

# Arc timestamps are seconds since the Apple reference date (2001-01-01 UTC).
APPLE_EPOCH_OFFSET = 978307200

INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")


@dataclass
class Bookmark:
    title: str
    url: str
    added_at: Optional[int] = None


@dataclass
class Folder:
    title: str
    children: List["Node"]
    added_at: Optional[int] = None


Node = Union[Bookmark, Folder]


@dataclass
class Space:
    title: str
    pinned_id: Optional[str]
    unpinned_id: Optional[str]


@dataclass
class ParseStats:
    missing_items: int = 0
    skipped_tabs: int = 0


def clean_text(value: Any) -> str:
    """Collapse whitespace and return an empty string for non-strings."""
    if not isinstance(value, str):
        return ""
    return WHITESPACE.sub(" ", value).strip()


def unix_timestamp(value: Any) -> Optional[int]:
    """Convert an Arc Apple-reference timestamp to Unix seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    return int(value + APPLE_EPOCH_OFFSET)


def load_sidebar(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"error: Arc sidebar file not found: {path}")
    except OSError as exc:
        raise SystemExit(f"error: could not read {path}: {exc}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"error: invalid JSON in {path}: {exc.msg} (line {exc.lineno}, "
            f"column {exc.colno}).\n"
            "Arc may be writing the file; quit Arc or try again."
        )

    if not isinstance(data, dict):
        raise SystemExit(f"error: unexpected Arc sidebar structure in {path}")
    return data


def iter_containers(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return sidebar containers that hold spaces and items."""
    containers = data.get("sidebar", {}).get("containers", [])
    if isinstance(containers, dict):
        containers = list(containers.values())
    if not isinstance(containers, list):
        return []

    return [
        container
        for container in containers
        if isinstance(container, dict)
        and isinstance(container.get("items"), (list, dict))
        and isinstance(container.get("spaces"), (list, dict))
    ]


def build_item_lookup(items: Any) -> Dict[str, Dict[str, Any]]:
    """Build an id -> item map from Arc's interleaved items list."""
    lookup: Dict[str, Dict[str, Any]] = {}

    if isinstance(items, dict):
        for item_id, item in items.items():
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("id", item_id)
                if isinstance(item.get("id"), str):
                    lookup[item["id"]] = item
        return lookup

    if not isinstance(items, list):
        return lookup

    for entry in items:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            lookup[entry["id"]] = entry
    return lookup


def find_section_ids(space_data: Dict[str, Any]) -> Dict[str, str]:
    """Map 'pinned' and 'unpinned' to their container ids.

    Newer Arc files use newContainerIDs, an alternating list of section marker
    objects and container ids:
        [{"pinned": {}}, "<pinned id>", {"unpinned": {}}, "<unpinned id>"]
    Older files use containerIDs with plain string labels:
        ["unpinned", "<id>", "pinned", "<id>"]
    """
    sections: Dict[str, str] = {}

    def add_legacy(entries: Any) -> None:
        if isinstance(entries, dict):
            for label in ("pinned", "unpinned"):
                container_id = entries.get(label)
                if isinstance(container_id, str):
                    sections.setdefault(label, container_id)
            return

        if not isinstance(entries, list):
            return

        for index, entry in enumerate(entries):
            if not isinstance(entry, str) or entry not in ("pinned", "unpinned"):
                continue
            if index + 1 < len(entries) and isinstance(entries[index + 1], str):
                sections.setdefault(entry, entries[index + 1])

    new_ids = space_data.get("newContainerIDs")
    if isinstance(new_ids, list):
        for index, entry in enumerate(new_ids):
            if not isinstance(entry, dict):
                continue
            for label in ("pinned", "unpinned"):
                if label not in entry:
                    continue
                if index + 1 < len(new_ids) and isinstance(new_ids[index + 1], str):
                    sections.setdefault(label, new_ids[index + 1])

    if len(sections) < 2:
        add_legacy(space_data.get("containerIDs"))

    return sections


def iter_spaces(container: Dict[str, Any]) -> List[Space]:
    """Extract spaces in sidebar order from one container."""
    raw_spaces = container.get("spaces", [])
    if isinstance(raw_spaces, dict):
        raw_spaces = list(raw_spaces.values())
    if not isinstance(raw_spaces, list):
        return []

    spaces: List[Space] = []
    unnamed = 0

    for entry in raw_spaces:
        if not isinstance(entry, dict):
            continue

        space_id = entry.get("id")
        if not isinstance(space_id, str):
            continue

        title = clean_text(entry.get("title"))
        if not title:
            unnamed += 1
            title = f"Space {unnamed}"

        sections = find_section_ids(entry)
        spaces.append(
            Space(
                title=title,
                pinned_id=sections.get("pinned"),
                unpinned_id=sections.get("unpinned"),
            )
        )

    return spaces


def tab_to_bookmark(
    item: Dict[str, Any], tab: Dict[str, Any], stats: ParseStats
) -> Optional[Bookmark]:
    url = tab.get("savedURL")
    if not isinstance(url, str) or not url.strip():
        stats.skipped_tabs += 1
        return None
    url = url.strip()

    # An item title is Arc's user-renamed title; fall back to the page title.
    title = clean_text(item.get("title"))
    if not title:
        title = clean_text(tab.get("savedTitle"))
    if not title:
        title = url

    return Bookmark(
        title=title,
        url=url,
        added_at=unix_timestamp(item.get("createdAt")),
    )


def walk_children(
    container_id: str,
    lookup: Dict[str, Dict[str, Any]],
    visited: Set[str],
    stats: ParseStats,
) -> List[Node]:
    """Walk childrenIds depth-first, preserving sidebar order."""
    children: List[Node] = []
    container = lookup.get(container_id)
    if container is None:
        stats.missing_items += 1
        return children

    for child_id in container.get("childrenIds") or []:
        if not isinstance(child_id, str) or child_id in visited:
            continue
        visited.add(child_id)

        item = lookup.get(child_id)
        if item is None:
            stats.missing_items += 1
            continue

        data = item.get("data")
        if not isinstance(data, dict):
            data = {}

        tab = data.get("tab")
        if isinstance(tab, dict):
            bookmark = tab_to_bookmark(item, tab, stats)
            if bookmark is not None:
                children.append(bookmark)
            continue

        if isinstance(data.get("list"), dict):
            folder_title = clean_text(item.get("title")) or "Untitled Folder"
            folder_children = walk_children(child_id, lookup, visited, stats)
            if folder_children:
                children.append(
                    Folder(
                        title=folder_title,
                        children=folder_children,
                        added_at=unix_timestamp(item.get("createdAt")),
                    )
                )
            continue

        # Split views (and unknown wrappers) hold tabs; flatten them in place.
        children.extend(walk_children(child_id, lookup, visited, stats))

    return children


def build_space_tree(
    space: Space,
    lookup: Dict[str, Dict[str, Any]],
    scope: str,
    stats: ParseStats,
) -> List[Node]:
    """Build the space contents for the requested scope.

    scope="pinned" exports only the pinned section. scope="all" exports the
    pinned section followed by the space's unpinned (open) tabs.
    """
    nodes: List[Node] = []

    if space.pinned_id:
        nodes.extend(walk_children(space.pinned_id, lookup, {space.pinned_id}, stats))

    if scope == "all" and space.unpinned_id:
        nodes.extend(
            walk_children(space.unpinned_id, lookup, {space.unpinned_id}, stats)
        )

    return nodes


def count_bookmarks(nodes: List[Node]) -> int:
    total = 0
    for node in nodes:
        if isinstance(node, Bookmark):
            total += 1
        else:
            total += count_bookmarks(node.children)
    return total


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def date_attr(name: str, value: Optional[int]) -> str:
    return f' {name}="{value}"' if value else ""


def render_items(nodes: List[Node], depth: int) -> List[str]:
    indent = "    " * depth
    lines: List[str] = []

    for node in nodes:
        if isinstance(node, Bookmark):
            lines.append(
                f'{indent}<DT><A HREF="{escape(node.url)}"'
                f'{date_attr("ADD_DATE", node.added_at)}>{escape(node.title)}</A>'
            )
            continue

        lines.append(
            f'{indent}<DT><H3{date_attr("ADD_DATE", node.added_at)}>'
            f"{escape(node.title)}</H3>"
        )
        lines.append(f"{indent}<DL><p>")
        lines.extend(render_items(node.children, depth + 1))
        lines.append(f"{indent}</DL><p>")

    return lines


def render_space_html(title: str, nodes: List[Node]) -> str:
    """Render one space as Netscape bookmark HTML.

    The space contents are wrapped in a folder named after the space so that
    importing several per-space files keeps the spaces separate.
    """
    escaped_title = escape(title)
    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        "<!-- This is an automatically generated file. -->",
        "<!-- It will be read and overwritten. -->",
        "<!-- DO NOT EDIT! -->",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        f"<TITLE>{escaped_title}</TITLE>",
        f"<H1>{escaped_title}</H1>",
        "<DL><p>",
        f"    <DT><H3>{escaped_title}</H3>",
        "    <DL><p>",
    ]
    lines.extend(render_items(nodes, depth=2))
    lines.extend(
        [
            "    </DL><p>",
            "</DL><p>",
        ]
    )
    return "\n".join(lines) + "\n"


def unique_labels(titles: List[str]) -> List[str]:
    """Disambiguate duplicate space names for readable imported folders."""
    used: Set[str] = set()
    labels: List[str] = []

    for title in titles:
        base = clean_text(title) or "Space"
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base} ({suffix})"
            suffix += 1
        used.add(candidate.casefold())
        labels.append(candidate)

    return labels


def safe_filename(title: str) -> str:
    """Turn a space title into a portable macOS/Linux/Windows filename stem."""
    name = INVALID_FILENAME_CHARS.sub("-", title)
    name = WHITESPACE.sub(" ", name).strip(" .-")
    if not name:
        name = "space"

    # Keep the filename comfortably below common 255-byte name limits.
    while len(name.encode("utf-8")) > 180:
        name = name[:-1].rstrip(" .-")
    return name or "space"


def unique_stems(titles: List[str]) -> List[str]:
    """Produce unique, case-insensitively distinct filename stems."""
    used: Set[str] = set()
    stems: List[str] = []

    for title in titles:
        base = safe_filename(title)
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base} ({suffix})"
            suffix += 1
        used.add(candidate.casefold())
        stems.append(candidate)

    return stems


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export each Arc space to its own NETSCAPE-Bookmark-file-1 HTML file, "
            "preserving Arc's folders."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s\n"
            "      Export pinned bookmarks from every space to ./arc-bookmarks.\n"
            "  %(prog)s --scope all\n"
            "      Also include each space's unpinned (open) tabs.\n"
            "  %(prog)s --space Work --space Research -o ~/Desktop/bookmarks\n"
            "      Export only the named spaces (case-insensitive).\n"
            "  %(prog)s --input ./StorableSidebar.json -o ./out\n"
            "      Export from a copy of Arc's data file.\n\n"
            "Arc-internal URLs (arc://, chrome-extension://) are exported as-is.\n"
            "Profile-level Favorites are not part of a space and are not included."
        ),
    )
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="path to StorableSidebar.json (default: %(default)s)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory for the per-space HTML files (default: %(default)s)",
    )
    parser.add_argument(
        "--scope",
        choices=("pinned", "all"),
        default="pinned",
        help=(
            "which bookmarks to export: 'pinned' for the pinned section only, "
            "'all' for pinned plus unpinned/open tabs (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--space",
        action="append",
        metavar="NAME",
        help=(
            "export only this space; repeat for multiple spaces "
            "(case-insensitive exact match)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    input_path = args.input.expanduser()
    output_dir = args.output_dir.expanduser()

    data = load_sidebar(input_path)
    containers = iter_containers(data)
    if not containers:
        raise SystemExit(f"error: no Arc spaces found in {input_path}")

    warnings: List[str] = []
    all_spaces: List[Tuple[Space, List[Node]]] = []
    stats = ParseStats()

    for container in containers:
        lookup = build_item_lookup(container.get("items"))
        for space in iter_spaces(container):
            if not space.pinned_id and not space.unpinned_id:
                warnings.append(
                    f"space {space.title!r}: no pinned or unpinned container found; "
                    "skipped"
                )
                continue
            all_spaces.append(
                (space, build_space_tree(space, lookup, args.scope, stats))
            )

    if not all_spaces:
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        raise SystemExit(f"error: no exportable Arc spaces found in {input_path}")

    if args.space:
        wanted = {name.strip().casefold() for name in args.space}
        available = ", ".join(repr(space.title) for space, _ in all_spaces) or "(none)"
        all_spaces = [
            (space, nodes)
            for space, nodes in all_spaces
            if space.title.casefold() in wanted
        ]
        if not all_spaces:
            raise SystemExit(
                "error: no space matched --space "
                f"{', '.join(repr(name) for name in args.space)}; "
                f"available spaces: {available}"
            )

    if output_dir.exists() and not output_dir.is_dir():
        raise SystemExit(f"error: output path is not a directory: {output_dir}")

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"error: could not create {output_dir}: {exc}")

    labels = unique_labels([space.title for space, _ in all_spaces])
    stems = unique_stems(labels)
    total_bookmarks = 0
    written: List[Tuple[str, int]] = []

    for (_space, nodes), label, stem in zip(all_spaces, labels, stems):
        path = output_dir / f"{stem}.html"
        try:
            path.write_text(render_space_html(label, nodes), encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"error: could not write {path}: {exc}")

        count = count_bookmarks(nodes)
        total_bookmarks += count
        written.append((path.name, count))

    scope_label = "pinned bookmarks" if args.scope == "pinned" else "bookmarks (pinned + unpinned)"
    print(
        f"Exported {len(written)} space(s), {total_bookmarks} {scope_label} "
        f"to {output_dir.resolve()}"
    )
    for filename, count in written:
        print(f"  {filename}: {count}")

    if stats.skipped_tabs:
        print(
            f"warning: skipped {stats.skipped_tabs} tab(s) without a saved URL",
            file=sys.stderr,
        )
    if stats.missing_items:
        print(
            f"warning: {stats.missing_items} referenced sidebar item(s) were missing",
            file=sys.stderr,
        )
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
