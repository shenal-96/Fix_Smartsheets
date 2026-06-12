"""
smartsheet_sync.py — Core logic for syncing checklist sheets from master
templates to generator copies. Importable; no printing, no argv parsing.

Used by both the CLI (sync_checklist.py) and the web app (app.py).
"""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import smartsheet


def _api_call(fn, *args, **kwargs):
    """Call fn(*args, **kwargs), retrying up to 5 times on 429 rate-limit errors."""
    delay = 2
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except smartsheet.exceptions.ApiError as exc:
            result = getattr(getattr(exc, "error", None), "result", None)
            status = getattr(result, "status_code", None) or getattr(result, "statusCode", None)
            if status == 429 and attempt < 5:
                time.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                raise


# ----------------------------- Data classes -----------------------------

@dataclass
class SheetRef:
    sheet_id: int
    name: str
    rel_path: Tuple[str, ...]


@dataclass
class RowData:
    row_id: Optional[int]
    cells_by_col_name: Dict[str, object]


@dataclass
class SheetPlan:
    master: SheetRef
    generator: SheetRef
    generator_folder: str
    key_column: str
    template_columns: List[str]
    gen_col_name_to_id: Dict[str, int]
    rows_to_add: List[RowData] = field(default_factory=list)
    rows_to_update: List[Tuple[RowData, RowData]] = field(default_factory=list)
    rows_to_delete: List[RowData] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.rows_to_add or self.rows_to_update or self.rows_to_delete)


# ----------------------------- Workspace traversal -----------------------------

def walk_folder(client, folder, rel_path: Tuple[str, ...]) -> List[SheetRef]:
    sheets: List[SheetRef] = []
    if folder.sheets:
        for s in folder.sheets:
            sheets.append(SheetRef(s.id, s.name, rel_path + (s.name,)))
    if folder.folders:
        for sub in folder.folders:
            sub_full = _api_call(client.Folders.get_folder, sub.id)
            sheets.extend(walk_folder(client, sub_full, rel_path + (sub.name,)))
    return sheets


def get_workspace_layout(
    client, workspace_id: int, templates_folder_name: str,
    client_factory: Optional[Callable[[], "smartsheet.Smartsheet"]] = None,
) -> Tuple[List[SheetRef], List[Tuple[str, List[SheetRef]]]]:
    """Returns (template_sheets, [(generator_folder_name, [sheets]) ...]).

    If ``client_factory`` is given, generator folders are walked in parallel,
    each thread using its own client (the SDK client is not thread-safe).
    Without it, folders are walked serially using the passed ``client``.
    """
    ws = _api_call(client.Workspaces.get_workspace, workspace_id)

    templates_folder = None
    generator_folders = []
    for f in (ws.folders or []):
        if f.name == templates_folder_name:
            templates_folder = f
        else:
            generator_folders.append(f)

    if templates_folder is None:
        raise RuntimeError(
            f"No folder named '{templates_folder_name}' at the top level of workspace {workspace_id}."
        )
    if not generator_folders:
        raise RuntimeError(
            f"No generator folders found in workspace {workspace_id} (only '{templates_folder_name}' exists)."
        )

    tf_full = _api_call(client.Folders.get_folder, templates_folder.id)
    template_sheets = walk_folder(client, tf_full, tuple())

    if client_factory and len(generator_folders) > 1:
        def _walk_gen(gf):
            c = client_factory()
            gf_full = _api_call(c.Folders.get_folder, gf.id)
            return (gf.name, walk_folder(c, gf_full, tuple()))

        with ThreadPoolExecutor(max_workers=min(len(generator_folders), 8)) as executor:
            generators = list(executor.map(_walk_gen, generator_folders))
    else:
        generators = []
        for gf in generator_folders:
            gf_full = _api_call(client.Folders.get_folder, gf.id)
            generators.append((gf.name, walk_folder(client, gf_full, tuple())))

    return template_sheets, generators


# ----------------------------- Sheet reading -----------------------------

def fetch_sheet(client, sheet_id: int):
    sheet = _api_call(client.Sheets.get_sheet, sheet_id)
    col_name_to_id = {c.title: c.id for c in sheet.columns}
    col_id_to_name = {c.id: c.title for c in sheet.columns}
    primary = next((c.title for c in sheet.columns if c.primary), None)
    return sheet, col_name_to_id, col_id_to_name, primary


_READONLY_SYSTEM_COLUMN_TYPES = frozenset({
    "AUTO_NUMBER", "MODIFIED_DATE", "MODIFIED_BY", "CREATED_DATE", "CREATED_BY"
})


def column_is_editable(col) -> bool:
    """A column is read-only if it is a system column or has a column formula."""
    # Column-formula columns are computed and reject direct cell edits (error 1302).
    # `formula` is a plain string: None/empty for normal columns.
    if getattr(col, "formula", None):
        return False
    # System columns (Auto Number, Created/Modified Date/By) are read-only.
    # The SDK exposes system_column_type as an EnumeratedValue (unhashable),
    # so normalise to a plain string before the set-membership check.
    system_type = getattr(col, "system_column_type", None)
    if system_type is None:
        return True
    return str(system_type) not in _READONLY_SYSTEM_COLUMN_TYPES


def row_to_data(row, col_id_to_name: Dict[int, str]) -> RowData:
    cells: Dict[str, object] = {}
    for cell in (row.cells or []):
        name = col_id_to_name.get(cell.column_id)
        if name is not None:
            cells[name] = cell.value
    return RowData(row_id=row.id, cells_by_col_name=cells)


def key_of(row: RowData, key_column: str) -> Optional[str]:
    val = row.cells_by_col_name.get(key_column)
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


# ----------------------------- Diffing -----------------------------

def compute_diff(
    master_rows: List[RowData],
    generator_rows: List[RowData],
    template_columns: List[str],
    key_column: str,
) -> Tuple[List[RowData], List[Tuple[RowData, RowData]], List[RowData], List[str]]:
    warnings: List[str] = []

    master_by_key: Dict[str, RowData] = {}
    dup_keys = set()
    for r in master_rows:
        k = key_of(r, key_column)
        if k is None:
            continue
        if k in master_by_key:
            dup_keys.add(k)
        master_by_key[k] = r
    for dk in dup_keys:
        warnings.append(f"Duplicate key in master: '{dk}' — only last occurrence will be used.")

    gen_by_key: Dict[str, RowData] = {}
    orphan = 0
    for r in generator_rows:
        k = key_of(r, key_column)
        if k is None:
            orphan += 1
            continue
        gen_by_key[k] = r
    if orphan:
        warnings.append(f"{orphan} generator row(s) have empty key — left alone (not deleted).")

    to_add = [m for k, m in master_by_key.items() if k not in gen_by_key]
    to_delete = [g for k, g in gen_by_key.items() if k not in master_by_key]
    to_update: List[Tuple[RowData, RowData]] = []
    for k, mrow in master_by_key.items():
        if k not in gen_by_key:
            continue
        grow = gen_by_key[k]
        for col in template_columns:
            if mrow.cells_by_col_name.get(col) != grow.cells_by_col_name.get(col):
                to_update.append((grow, mrow))
                break

    return to_add, to_update, to_delete, warnings


# ----------------------------- Plan building -----------------------------

def build_plans(
    client,
    template_sheets: List[SheetRef],
    generators: List[Tuple[str, List[SheetRef]]],
    instance_columns: List[str],
    key_column_override: Optional[str],
    allow_empty_master: bool = False,
) -> Tuple[List[SheetPlan], List[str]]:
    """Returns (plans, warnings)."""
    plans: List[SheetPlan] = []
    warnings: List[str] = []
    instance_set = set(instance_columns)

    for master in template_sheets:
        master_sheet, _, master_col_id_to_name, master_primary = fetch_sheet(client, master.sheet_id)

        key_column = key_column_override or master_primary
        if key_column is None:
            warnings.append(f"Master '{'/'.join(master.rel_path)}' has no primary column; skipping.")
            continue

        all_cols = list({c.title for c in master_sheet.columns})
        template_columns = [c for c in all_cols if c not in instance_set]

        master_rows = [row_to_data(r, master_col_id_to_name) for r in (master_sheet.rows or [])]

        if not master_rows and not allow_empty_master:
            warnings.append(
                f"Master '{'/'.join(master.rel_path)}' has 0 rows; skipped to prevent mass deletion."
            )
            continue

        for gen_folder_name, gen_sheets in generators:
            match = next((s for s in gen_sheets if s.rel_path == master.rel_path), None)
            if match is None:
                warnings.append(
                    f"No matching sheet for '{'/'.join(master.rel_path)}' in '{gen_folder_name}'."
                )
                continue

            gen_sheet, gen_col_name_to_id, gen_col_id_to_name, _ = fetch_sheet(client, match.sheet_id)

            missing = [c for c in template_columns if c not in gen_col_name_to_id]
            if missing:
                warnings.append(
                    f"'{gen_folder_name}/{'/'.join(match.rel_path)}' missing columns: {missing} (skipped on this sheet)."
                )

            gen_rows = [row_to_data(r, gen_col_id_to_name) for r in (gen_sheet.rows or [])]
            to_add, to_update, to_delete, w = compute_diff(master_rows, gen_rows, template_columns, key_column)
            for x in w:
                warnings.append(f"[{gen_folder_name}] {x}")

            plan = SheetPlan(
                master=master,
                generator=match,
                generator_folder=gen_folder_name,
                key_column=key_column,
                template_columns=template_columns,
                gen_col_name_to_id=gen_col_name_to_id,
                rows_to_add=to_add,
                rows_to_update=to_update,
                rows_to_delete=to_delete,
            )
            if not plan.is_empty:
                plans.append(plan)

    return plans, warnings


# ----------------------------- Apply -----------------------------

def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def apply_plan(client, plan: SheetPlan) -> None:
    sheet_id = plan.generator.sheet_id

    if plan.rows_to_add:
        new_rows = []
        for source in plan.rows_to_add:
            row = smartsheet.models.Row()
            row.to_bottom = True
            for col in plan.template_columns:
                if col in plan.gen_col_name_to_id and col in source.cells_by_col_name:
                    cell = smartsheet.models.Cell()
                    cell.column_id = plan.gen_col_name_to_id[col]
                    cell.value = source.cells_by_col_name[col]
                    row.cells.append(cell)
            new_rows.append(row)
        for batch in _chunked(new_rows, 200):
            _api_call(client.Sheets.add_rows, sheet_id, batch)

    if plan.rows_to_update:
        upd = []
        for existing, source in plan.rows_to_update:
            row = smartsheet.models.Row()
            row.id = existing.row_id
            for col in plan.template_columns:
                if col in plan.gen_col_name_to_id and col in source.cells_by_col_name:
                    cell = smartsheet.models.Cell()
                    cell.column_id = plan.gen_col_name_to_id[col]
                    cell.value = source.cells_by_col_name[col]
                    row.cells.append(cell)
            upd.append(row)
        for batch in _chunked(upd, 200):
            _api_call(client.Sheets.update_rows, sheet_id, batch)

    if plan.rows_to_delete:
        ids = [r.row_id for r in plan.rows_to_delete]
        for batch in _chunked(ids, 200):
            _api_call(client.Sheets.delete_rows, sheet_id, batch)


# ----------------------------- Serialization for web UI -----------------------------

def plan_to_dict(plan: SheetPlan, index: int) -> dict:
    """Convert a SheetPlan to a JSON-friendly dict for the web UI."""
    return {
        "id": index,
        "generator_folder": plan.generator_folder,
        "generator_path": "/".join(plan.generator.rel_path),
        "master_path": "/".join(plan.master.rel_path),
        "key_column": plan.key_column,
        "template_columns": plan.template_columns,
        "counts": {
            "add": len(plan.rows_to_add),
            "update": len(plan.rows_to_update),
            "delete": len(plan.rows_to_delete),
        },
        "rows_to_add": [
            {"key": key_of(r, plan.key_column), "values": _stringify_values(r.cells_by_col_name)}
            for r in plan.rows_to_add
        ],
        "rows_to_update": [
            {
                "key": key_of(source, plan.key_column),
                "diffs": [
                    {
                        "column": col,
                        "old": _stringify(existing.cells_by_col_name.get(col)),
                        "new": _stringify(source.cells_by_col_name.get(col)),
                    }
                    for col in plan.template_columns
                    if existing.cells_by_col_name.get(col) != source.cells_by_col_name.get(col)
                ],
            }
            for existing, source in plan.rows_to_update
        ],
        "rows_to_delete": [
            {"key": key_of(r, plan.key_column), "values": _stringify_values(r.cells_by_col_name)}
            for r in plan.rows_to_delete
        ],
    }


def _stringify(v):
    if v is None:
        return ""
    return str(v)


def _stringify_values(d: Dict[str, object]) -> Dict[str, str]:
    return {k: _stringify(v) for k, v in d.items()}


# ----------------------------- New generator (copy templates) -----------------------------

def _copy_folder_contents(client, source_folder, dest_folder_id: int) -> int:
    """Recursively copy sheets and subfolders from source into dest_folder_id.

    Returns the total number of sheets copied.
    """
    count = 0
    for sheet in source_folder.sheets or []:
        dest = smartsheet.models.ContainerDestination()
        dest.destination_type = "folder"
        dest.destination_id = dest_folder_id
        dest.new_name = sheet.name
        _api_call(client.Sheets.copy_sheet, sheet.id, dest)
        count += 1

    for subfolder in source_folder.folders or []:
        new_sub = smartsheet.models.Folder()
        new_sub.name = subfolder.name
        result = _api_call(client.Folders.create_folder, dest_folder_id, new_sub)
        new_sub_id = result.result.id

        full_sub = _api_call(client.Folders.get_folder, subfolder.id)
        count += _copy_folder_contents(client, full_sub, new_sub_id)

    return count


def create_generator_from_templates(
    client,
    workspace_id: int,
    templates_folder_name: str,
    new_folder_name: str,
) -> int:
    """Create a new generator folder by copying the Templates folder structure.

    Returns the number of sheets copied.
    """
    ws = _api_call(client.Workspaces.get_workspace, workspace_id)

    templates_folder = next(
        (f for f in (ws.folders or []) if f.name == templates_folder_name), None
    )
    if templates_folder is None:
        raise RuntimeError(
            f"No folder named '{templates_folder_name}' found in workspace {workspace_id}."
        )

    # Check for name collision
    existing_names = {f.name for f in (ws.folders or [])}
    if new_folder_name in existing_names:
        raise RuntimeError(
            f"A folder named '{new_folder_name}' already exists in this workspace."
        )

    new_folder = smartsheet.models.Folder()
    new_folder.name = new_folder_name
    result = _api_call(client.Workspaces.create_folder, workspace_id, new_folder)
    new_folder_id = result.result.id

    tf_full = _api_call(client.Folders.get_folder, templates_folder.id)
    return _copy_folder_contents(client, tf_full, new_folder_id)


# ----------------------------- Workspace discovery -----------------------------

def list_workspaces(client) -> List[Tuple[str, int]]:
    """Return [(workspace_name, workspace_id), ...] for every workspace the
    API token can access, sorted by name (case-insensitive).

    Lets the UI offer a searchable picker instead of asking the user to paste a
    raw workspace ID.
    """
    result: List[Tuple[str, int]] = []
    try:
        # SDK 3.x: one call with include_all returns every workspace.
        resp = _api_call(client.Workspaces.list_workspaces, include_all=True)
        for w in (getattr(resp, "data", None) or []):
            result.append((w.name, w.id))
    except TypeError:
        # SDK 4.x dropped include_all in favour of token-based pagination.
        last_key = None
        while True:
            resp = _api_call(client.Workspaces.list_workspaces, last_key=last_key)
            for w in (getattr(resp, "data", None) or []):
                result.append((w.name, w.id))
            last_key = getattr(resp, "last_key", None)
            if not last_key:
                break
    result.sort(key=lambda t: (t[0] or "").lower())
    return result


# ----------------------------- Row Editor helpers -----------------------------

def list_workspace_folders(client, workspace_id: int) -> List[Tuple[str, int]]:
    """Return [(folder_name, folder_id), ...] for every top-level folder in the workspace."""
    ws = _api_call(client.Workspaces.get_workspace, workspace_id)
    return [(f.name, f.id) for f in (ws.folders or [])]


def list_sheets_in_folder(client, folder_id: int) -> List[SheetRef]:
    """Return a flat list of all SheetRefs in a folder tree."""
    folder = _api_call(client.Folders.get_folder, folder_id)
    return walk_folder(client, folder, tuple())


def fetch_row_by_number(
    client, sheet_id: int, row_number: int
) -> Tuple[RowData, Dict[str, int], List[str], Dict[str, bool]]:
    """Fetch one row by 1-based position.

    Returns (row_data, col_name_to_id, ordered_col_names, col_name_to_editable).
    """
    sheet, col_name_to_id, col_id_to_name, _ = fetch_sheet(client, sheet_id)
    rows = sheet.rows or []
    if not rows:
        raise ValueError("Sheet has no rows.")
    if row_number < 1 or row_number > len(rows):
        raise ValueError(
            f"Row {row_number} is out of range (sheet has {len(rows)} rows)."
        )
    row = rows[row_number - 1]
    row_data = row_to_data(row, col_id_to_name)
    col_names_ordered = [c.title for c in sheet.columns]
    col_name_to_editable = {c.title: column_is_editable(c) for c in sheet.columns}
    return row_data, col_name_to_id, col_names_ordered, col_name_to_editable


def update_row_cells(
    client,
    sheet_id: int,
    row_id: int,
    col_name_to_id: Dict[str, int],
    updates: Dict[str, str],
    col_name_to_type: Optional[Dict[str, str]] = None,
    col_name_to_editable: Optional[Dict[str, bool]] = None,
    original_values: Optional[Dict[str, object]] = None,
) -> None:
    """Write cell updates to a row.

    For each editable column in ``updates``:
      * a non-empty value is written (CHECKBOX strings are coerced to bool);
      * a blank value *clears* the cell, but only if it currently holds
        content -- an explicit empty string is sent so Smartsheet wipes the
        value (the SDK/API does nothing when a blank value is simply omitted);
      * a blank value whose cell is already empty is skipped, so we never send
        a pointless no-op clear.

    ``original_values`` maps column name -> current cell value and is used to
    decide whether a blank entry is a deliberate clear. When it is not supplied
    blank entries are skipped, preserving the previous "only send content"
    behaviour for callers that cannot provide the current state.

    Skips system/formula columns regardless of value.
    """
    row = smartsheet.models.Row()
    row.id = row_id
    for col_name, new_value in updates.items():
        if col_name not in col_name_to_id:
            continue
        # Never write to system or formula columns.
        if col_name_to_editable is not None and not col_name_to_editable.get(col_name, True):
            continue

        val = (new_value or "").strip()
        cell = smartsheet.models.Cell()
        cell.column_id = col_name_to_id[col_name]

        if not val:
            # Blank entry -- only act if the cell currently has content to clear.
            had_content = False
            if original_values is not None:
                prev = original_values.get(col_name)
                had_content = prev is not None and str(prev).strip() != ""
            if not had_content:
                continue
            # An explicit empty string is what actually clears a Smartsheet cell.
            cell.value = ""
        else:
            # Convert value based on column type
            col_type = col_name_to_type.get(col_name) if col_name_to_type else None
            if col_type == "CHECKBOX":
                cell.value = val.lower() in ("true", "1", "yes", "checked")
            else:
                cell.value = val

        row.cells.append(cell)
    if row.cells:
        _api_call(client.Sheets.update_rows, sheet_id, [row])


def fetch_sheet_columns(
    client, sheet_id: int
) -> Tuple[Dict[str, int], List[str], Dict[str, str], Dict[str, bool]]:
    """Return (col_name_to_id, ordered_col_names, col_name_to_type, col_name_to_editable)."""
    sheet, col_name_to_id, _, _ = fetch_sheet(client, sheet_id)
    col_names_ordered = [c.title for c in sheet.columns]
    col_name_to_type = {c.title: str(c.type) for c in sheet.columns}
    col_name_to_editable = {c.title: column_is_editable(c) for c in sheet.columns}
    return col_name_to_id, col_names_ordered, col_name_to_type, col_name_to_editable


def delete_row_by_number(client, sheet_id: int, row_number: int) -> None:
    """Delete one row (1-based position) from a sheet."""
    sheet = _api_call(client.Sheets.get_sheet, sheet_id)
    rows = sheet.rows or []
    if not rows:
        raise ValueError("Sheet has no rows.")
    if row_number < 1 or row_number > len(rows):
        raise ValueError(
            f"Row {row_number} is out of range (sheet has {len(rows)} rows)."
        )
    row_id = rows[row_number - 1].id
    _api_call(client.Sheets.delete_rows, sheet_id, [row_id])


def add_row_to_sheet(
    client,
    sheet_id: int,
    col_name_to_id: Dict[str, int],
    values: Dict[str, str],
    col_name_to_type: Optional[Dict[str, str]] = None,
    col_name_to_editable: Optional[Dict[str, bool]] = None,
    position: str = "bottom",
    sibling_row_number: Optional[int] = None,
) -> None:
    """Insert a new row into a sheet. Blank values are skipped.

    Positioning is controlled by ``position``:
      * ``"bottom"`` (default) -- append at the bottom of the sheet.
      * ``"top"``    -- insert at the top of the sheet.
      * ``"above"``  -- insert directly above the row at 1-based
                        ``sibling_row_number``.
      * ``"below"``  -- insert directly below the row at 1-based
                        ``sibling_row_number``.

    Skips system/formula columns and converts CHECKBOX values to booleans.
    """
    row = smartsheet.models.Row()

    if position in ("above", "below"):
        if sibling_row_number is None:
            raise ValueError(
                "sibling_row_number is required when position is 'above' or 'below'."
            )
        sheet = _api_call(client.Sheets.get_sheet, sheet_id)
        rows = sheet.rows or []
        if not rows:
            raise ValueError("Sheet has no rows to position relative to.")
        if sibling_row_number < 1 or sibling_row_number > len(rows):
            raise ValueError(
                f"Row {sibling_row_number} is out of range "
                f"(sheet has {len(rows)} rows)."
            )
        row.sibling_id = rows[sibling_row_number - 1].id
        row.above = position == "above"
    elif position == "top":
        row.to_top = True
    else:
        row.to_bottom = True

    for col_name, value in values.items():
        if col_name not in col_name_to_id:
            continue
        # Never write to system or formula columns.
        if col_name_to_editable is not None and not col_name_to_editable.get(col_name, True):
            continue
        val = (value or "").strip()
        if not val:
            continue
        cell = smartsheet.models.Cell()
        cell.column_id = col_name_to_id[col_name]

        col_type = col_name_to_type.get(col_name) if col_name_to_type else None
        if col_type == "CHECKBOX":
            cell.value = val.lower() in ("true", "1", "yes", "checked")
        else:
            cell.value = val

        row.cells.append(cell)
    if row.cells:
        _api_call(client.Sheets.add_rows, sheet_id, [row])


# ----------------------------- Large workspace copy -----------------------------
#
# Smartsheet's "Copy Workspace" API refuses workspaces above an item limit.
# As a workaround we create a fresh empty workspace and copy each top-level
# folder into it individually (copy_folder copies the whole sub-tree in one
# call, so each operation stays well under the limit). If a *single* folder is
# itself too large, we recurse: recreate it empty and copy its children one by
# one.
#
# Caveat (inherent to piecewise copying): cell links / cross-sheet references /
# dashboards that point ACROSS folders are only re-mapped within a single copy
# operation, so cross-folder links will still point at the ORIGINAL workspace.
# Content within any one folder is copied and re-linked correctly.

# What Smartsheet's copy `include` parameter accepts, with friendly labels.
COPY_INCLUDE_OPTIONS = {
    "Row data": "data",
    "Attachments": "attachments",
    "Comments": "discussions",
    "Forms": "forms",
    "Automation rules": "rules",
    "Cell links": "cellLinks",
    "Sharing": "shares",
}
COPY_INCLUDE_DEFAULT_LABELS = ["Row data", "Attachments", "Comments", "Forms"]

# How deep the recursive "split an oversized folder" fallback will go.
_MAX_SPLIT_DEPTH = 6


@dataclass
class CopyReport:
    new_workspace_id: Optional[int] = None
    new_workspace_name: str = ""
    permalink: str = ""
    copied: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)  # (item, reason)
    warnings: List[str] = field(default_factory=list)


def labels_to_include_tokens(labels: List[str]) -> List[str]:
    """Map friendly include labels to Smartsheet API tokens."""
    return [COPY_INCLUDE_OPTIONS[l] for l in labels if l in COPY_INCLUDE_OPTIONS]


def _container_dest(dest_type: str, dest_id: int, new_name: Optional[str] = None):
    cd = smartsheet.models.ContainerDestination()
    cd.destination_type = dest_type
    cd.destination_id = dest_id
    if new_name:
        cd.new_name = new_name
    return cd


def create_workspace(client, name: str):
    """Create a new empty workspace and return the Workspace object."""
    ws = smartsheet.models.Workspace()
    ws.name = name
    return client.Workspaces.create_workspace(ws).result


def _create_folder(client, dest_type: str, dest_id: int, name: str):
    folder = smartsheet.models.Folder()
    folder.name = name
    if dest_type == "workspace":
        return client.Workspaces.create_folder_in_workspace(dest_id, folder).result
    return client.Folders.create_folder_in_folder(dest_id, folder).result


def _copy_sheet_into(client, sheet_id, dest_type, dest_id, new_name, include):
    return client.Sheets.copy_sheet(
        sheet_id, _container_dest(dest_type, dest_id, new_name), include=include
    )


def _copy_folder_into(client, folder_id, dest_type, dest_id, new_name, include):
    return client.Folders.copy_folder(
        folder_id, _container_dest(dest_type, dest_id, new_name), include=include
    )


def _is_size_error(exc) -> bool:
    """Best-effort detection of the 'copy is too large' API error."""
    msg = str(exc).lower()
    return any(
        k in msg
        for k in ("maximum", "too large", "exceeds", "too many", "limit")
    )


def summarize_workspace(client, workspace_id: int) -> dict:
    """Lightweight preview of what a copy would move (one call per top folder)."""
    ws = client.Workspaces.get_workspace(workspace_id)
    folder_infos = []
    total_sheets = len(ws.sheets or [])
    for f in (ws.folders or []):
        full = client.Folders.get_folder(f.id)
        count = len(walk_folder(client, full, tuple()))
        folder_infos.append((f.name, count))
        total_sheets += count
    return {
        "workspace_name": ws.name,
        "folders": folder_infos,
        "top_level_sheets": [s.name for s in (ws.sheets or [])],
        "top_level_sights": len(ws.sights or []),
        "top_level_reports": len(ws.reports or []),
        "total_sheets": total_sheets,
    }


def _copy_folder_with_fallback(
    client, folder_summary, dest_type, dest_id, include, report, emit, depth=0
):
    name = folder_summary.name
    try:
        _copy_folder_into(client, folder_summary.id, dest_type, dest_id, name, include)
        report.copied.append(f"folder: {name}")
        emit(f"Copied folder '{name}'.")
        return
    except Exception as e:
        if not _is_size_error(e) or depth >= _MAX_SPLIT_DEPTH:
            report.failed.append((f"folder: {name}", str(e)))
            emit(f"FAILED folder '{name}': {e}")
            return
        emit(f"Folder '{name}' exceeds the copy limit -- splitting into smaller pieces...")

    # Oversized folder: recreate it empty and copy its children individually.
    try:
        new_folder = _create_folder(client, dest_type, dest_id, name)
    except Exception as e:
        report.failed.append((f"folder: {name} (could not recreate for split)", str(e)))
        emit(f"FAILED to recreate folder '{name}' for splitting: {e}")
        return

    full = client.Folders.get_folder(folder_summary.id)
    for sub in (full.folders or []):
        _copy_folder_with_fallback(
            client, sub, "folder", new_folder.id, include, report, emit, depth + 1
        )
    for sh in (full.sheets or []):
        try:
            _copy_sheet_into(client, sh.id, "folder", new_folder.id, sh.name, include)
            report.copied.append(f"sheet: {name}/{sh.name}")
            emit(f"Copied sheet '{name}/{sh.name}'.")
        except Exception as e:
            report.failed.append((f"sheet: {name}/{sh.name}", str(e)))
            emit(f"FAILED sheet '{name}/{sh.name}': {e}")
    if full.sights:
        report.warnings.append(
            f"{len(full.sights)} dashboard(s) in oversized folder '{name}' were not copied individually."
        )
    if full.reports:
        report.warnings.append(
            f"{len(full.reports)} report(s) in oversized folder '{name}' were not copied individually."
        )


def copy_workspace_piecewise(
    client,
    source_workspace_id: int,
    new_workspace_name: str,
    include_labels: Optional[List[str]] = None,
    progress=None,
) -> CopyReport:
    """Copy a (large) workspace into a new one, folder by folder.

    `progress` is an optional callable(str) invoked with human-readable status
    lines as the copy proceeds.
    """
    include = labels_to_include_tokens(
        include_labels if include_labels is not None else COPY_INCLUDE_DEFAULT_LABELS
    )

    def emit(msg: str) -> None:
        if progress:
            progress(msg)

    if not new_workspace_name.strip():
        raise ValueError("New workspace name is required.")

    ws = client.Workspaces.get_workspace(source_workspace_id)
    report = CopyReport(new_workspace_name=new_workspace_name.strip())

    new_ws = create_workspace(client, new_workspace_name.strip())
    report.new_workspace_id = new_ws.id
    report.permalink = getattr(new_ws, "permalink", "") or ""
    emit(f"Created new workspace '{report.new_workspace_name}' (id {new_ws.id}).")

    # Top-level folders (each copied as a whole sub-tree, with split fallback).
    for f in (ws.folders or []):
        _copy_folder_with_fallback(client, f, "workspace", new_ws.id, include, report, emit)

    # Sheets sitting directly at the workspace root (not inside any folder).
    for s in (ws.sheets or []):
        try:
            _copy_sheet_into(client, s.id, "workspace", new_ws.id, s.name, include)
            report.copied.append(f"sheet: {s.name}")
            emit(f"Copied top-level sheet '{s.name}'.")
        except Exception as e:
            report.failed.append((f"sheet: {s.name}", str(e)))
            emit(f"FAILED top-level sheet '{s.name}': {e}")

    # Dashboards/reports living at the workspace root cannot be copied across
    # workspaces piecewise (those inside folders are handled by copy_folder).
    if ws.sights:
        report.warnings.append(
            f"{len(ws.sights)} dashboard(s) at the workspace root were NOT copied "
            "(Smartsheet's API can't copy dashboards into a different workspace). Recreate them manually."
        )
    if ws.reports:
        report.warnings.append(
            f"{len(ws.reports)} report(s) at the workspace root were NOT copied. Recreate them manually."
        )

    emit("Done.")
    return report
