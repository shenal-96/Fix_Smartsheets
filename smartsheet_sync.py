"""
smartsheet_sync.py — Core logic for syncing checklist sheets from master
templates to generator copies. Importable; no printing, no argv parsing.

Used by both the CLI (sync_checklist.py) and the web app (app.py).
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import smartsheet


# ----------------------------- Rate-limit retry -----------------------------

def _call_with_retry(fn, *args, max_retries: int = 6, **kwargs):
    """Call fn(*args, **kwargs), retrying on Smartsheet 429 rate-limit errors
    with exponential backoff (2 s → 4 s → 8 s … up to 64 s)."""
    delay = 2
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except smartsheet.exceptions.ApiError as e:
            result = getattr(getattr(e, "error", None), "result", None)
            should_retry = getattr(result, "should_retry", False)
            if should_retry and attempt < max_retries:
                time.sleep(delay)
                delay = min(delay * 2, 64)
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
            sub_full = _call_with_retry(client.Folders.get_folder, sub.id)
            sheets.extend(walk_folder(client, sub_full, rel_path + (sub.name,)))
    return sheets


def get_workspace_layout(
    client, workspace_id: int, templates_folder_name: str
) -> Tuple[List[SheetRef], List[Tuple[str, List[SheetRef]]]]:
    """Returns (template_sheets, [(generator_folder_name, [sheets]) ...])."""
    ws = _call_with_retry(client.Workspaces.get_workspace, workspace_id)

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

    tf_full = _call_with_retry(client.Folders.get_folder, templates_folder.id)
    template_sheets = walk_folder(client, tf_full, tuple())

    generators = []
    for gf in generator_folders:
        gf_full = _call_with_retry(client.Folders.get_folder, gf.id)
        generators.append((gf.name, walk_folder(client, gf_full, tuple())))

    return template_sheets, generators


# ----------------------------- Sheet reading -----------------------------

def fetch_sheet(client, sheet_id: int):
    sheet = _call_with_retry(client.Sheets.get_sheet, sheet_id)
    col_name_to_id = {c.title: c.id for c in sheet.columns}
    col_id_to_name = {c.id: c.title for c in sheet.columns}
    primary = next((c.title for c in sheet.columns if c.primary), None)
    return sheet, col_name_to_id, col_id_to_name, primary


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
    instance_rows: Optional[List[str]] = None,
) -> Tuple[List[RowData], List[Tuple[RowData, RowData]], List[RowData], List[str]]:
    warnings: List[str] = []
    instance_row_set = set(instance_rows) if instance_rows else set()

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
    to_delete = [g for k, g in gen_by_key.items() if k not in master_by_key and k not in instance_row_set]
    to_update: List[Tuple[RowData, RowData]] = []
    for k, mrow in master_by_key.items():
        if k not in gen_by_key or k in instance_row_set:
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
    instance_rows: Optional[List[str]] = None,
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
            to_add, to_update, to_delete, w = compute_diff(master_rows, gen_rows, template_columns, key_column, instance_rows)
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
            _call_with_retry(client.Sheets.add_rows, sheet_id, batch)

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
            _call_with_retry(client.Sheets.update_rows, sheet_id, batch)

    if plan.rows_to_delete:
        ids = [r.row_id for r in plan.rows_to_delete]
        for batch in _chunked(ids, 200):
            _call_with_retry(client.Sheets.delete_rows, sheet_id, batch)


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
