"""
streamlit_app.py -- Streamlit UI for Smartsheet Checklist Sync.

Designed for deployment on Streamlit Community Cloud:
  https://share.streamlit.io

Local run (only needed for development):
  pip install -r requirements.txt
  streamlit run streamlit_app.py
"""

from datetime import datetime
from typing import Optional

import streamlit as st
import smartsheet

import smartsheet_sync as sync
import auth

# ============================================================
# Page config
# ============================================================
st.set_page_config(
    page_title="SmartSheets Editor",
    page_icon="checkmark",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# Helpers
# ============================================================
def get_secret(key: str, default: str = "") -> str:
    """Safely read a Streamlit secret. Returns default if not configured."""
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def save_user_prefs() -> None:
    """Persist the logged-in user's own (encrypted) token + workspace."""
    username = st.session_state.get("auth_username")
    if not username:
        return
    auth.save_user_secrets(
        username,
        st.session_state.get("cfg_api_token", ""),
        st.session_state.get("cfg_workspace_id", ""),
        get_secret,
    )


def init_state(username: str) -> None:
    if "plans" not in st.session_state:
        stored_token, stored_workspace = auth.load_user_secrets(username, get_secret)

        st.session_state.cfg_api_token = stored_token
        st.session_state.cfg_workspace_id = (
            st.query_params.get("wid", "")
            or stored_workspace
            or str(get_secret("DEFAULT_WORKSPACE_ID", ""))
        )
        st.session_state.cfg_templates_folder = "Templates"
        st.session_state.cfg_instance_columns = (
            "Check Status\nSigned Off By\nDate Completed\nComments"
        )
        st.session_state.cfg_key_column = ""
        st.session_state.plans = []
        st.session_state.scan_data = None
        st.session_state.scan_timestamp = None
        st.session_state.confirm_apply = False
        st.session_state.apply_result = None

    # Row Editor / Add Row state -- safe to call on every rerun via setdefault
    st.session_state.setdefault("fr_folders_list", None)
    st.session_state.setdefault("fr_sheets_by_folder", {})
    st.session_state.setdefault("fr_loaded_row", None)
    st.session_state.setdefault("fr_loaded_col_names", [])
    st.session_state.setdefault("fr_col_name_to_id", {})
    st.session_state.setdefault("fr_row_number", 1)
    st.session_state.setdefault("fr_apply_result", None)
    st.session_state.setdefault("fr_add_col_names", [])
    st.session_state.setdefault("fr_add_col_name_to_id", {})
    st.session_state.setdefault("fr_add_result", None)
    st.session_state.setdefault("fr_col_name_to_type", {})
    st.session_state.setdefault("fr_col_name_to_editable", {})
    st.session_state.setdefault("fr_add_col_name_to_editable", {})
    st.session_state.setdefault("fr_add_position", "Bottom of sheet")
    st.session_state.setdefault("fr_add_sibling_row_number", 1)
    st.session_state.setdefault("fr_del_loaded_row", None)
    st.session_state.setdefault("fr_del_col_names", [])
    st.session_state.setdefault("fr_del_row_number", 1)
    st.session_state.setdefault("fr_del_result", None)
    # Copy Workspace state
    st.session_state.setdefault("cw_preview", None)
    st.session_state.setdefault("cw_confirm", False)
    st.session_state.setdefault("cw_report", None)
    st.session_state.setdefault("cw_log", [])


def reset_results() -> None:
    st.session_state.plans = []
    st.session_state.scan_data = None
    st.session_state.scan_timestamp = None
    st.session_state.confirm_apply = False
    st.session_state.apply_result = None


# ============================================================
# Authentication -- each user signs in and uses their OWN API key
# ============================================================
authenticator, username = auth.require_login(get_secret)
st.session_state.auth_username = username

init_state(username)


# ============================================================
# Custom CSS
# ============================================================
st.markdown(
    """
    <style>
    .stApp { background-color: #F6F2E8; }
    h1 { font-weight: 600 !important; letter-spacing: -0.02em; }
    h2, h3 { letter-spacing: -0.01em; }
    section[data-testid="stSidebar"] { background-color: #FBF8F0; border-right: 1px solid #E0D8C2; }
    div[data-testid="stMetricValue"] { font-size: 1.9rem; font-weight: 600; }
    div[data-testid="stMetricLabel"] { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; }
    .diff-add    { background: #E9F0E9; color: #2F6A4E; padding: 4px 8px; border-left: 3px solid #2F6A4E; font-family: monospace; font-size: 0.875rem; margin: 2px 0; border-radius: 3px; }
    .diff-update { background: #F4ECD8; color: #9C6A2E; padding: 4px 8px; border-left: 3px solid #9C6A2E; font-family: monospace; font-size: 0.875rem; margin: 2px 0; border-radius: 3px; }
    .diff-delete { background: #F1DDDF; color: #97384A; padding: 4px 8px; border-left: 3px solid #97384A; font-family: monospace; font-size: 0.875rem; margin: 2px 0; border-radius: 3px; }
    .diff-detail { font-family: monospace; font-size: 0.8rem; color: #3F4655; margin: 2px 0 6px 18px; }
    .diff-detail .old { color: #97384A; text-decoration: line-through; }
    .diff-detail .new { color: #2F6A4E; }
    .diff-detail .col { color: #6F7585; }
    div.stButton > button[kind="primary"] { background-color: #234E7A; border-color: #234E7A; }
    div.stButton > button[kind="primary"]:hover { background-color: #16365A; border-color: #16365A; }
    .scan-meta { color: #6F7585; font-family: monospace; font-size: 0.8rem; padding-top: 12px; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# Sidebar -- settings
# ============================================================
with st.sidebar:
    st.markdown("## SmartSheets Editor")
    st.caption("Smartsheet master -> generator copies")

    name = st.session_state.get("name", username)
    st.markdown(f"Signed in as **{name}**")
    authenticator.logout("Log out", location="sidebar")
    st.divider()

    st.markdown("### Connection")
    st.caption("This is *your* personal Smartsheet API key. It is encrypted and never shared with other users.")
    st.text_input(
        "Smartsheet API token",
        type="password",
        key="cfg_api_token",
        on_change=save_user_prefs,
        help="Generate in Smartsheet: Account -> Personal Settings -> API Access.",
    )
    api_token = st.session_state.cfg_api_token

    st.markdown("### Project")
    st.text_input(
        "Workspace ID",
        key="cfg_workspace_id",
        on_change=save_user_prefs,
        help="The project workspace ID. Right-click workspace in Smartsheet -> Properties.",
    )
    workspace_id = st.session_state.cfg_workspace_id
    if workspace_id.strip():
        st.query_params["wid"] = workspace_id.strip()
    elif "wid" in st.query_params:
        del st.query_params["wid"]

    with st.expander("Advanced settings", expanded=False):
        templates_folder = st.text_input(
            "Templates folder name",
            key="cfg_templates_folder",
            help="Top-level folder holding master sheets.",
        )
        instance_columns_text = st.text_area(
            "Instance columns (one per line)",
            key="cfg_instance_columns",
            help="Columns that are per-generator and must NOT be overwritten.",
            height=120,
        )
        key_column_input = st.text_input(
            "Key column override",
            key="cfg_key_column",
            placeholder="(use primary column)",
            help="Leave blank to match rows on the primary column.",
        )

    st.divider()
    with st.expander("About", expanded=False):
        st.markdown(
            """
            Edit the master in `Templates/` -> run a scan -> apply.
            Instance columns (status, signoff, date, comments) are never touched.

            See the README in the GitHub repo for setup details.
            """
        )


# ============================================================
# Main area -- header
# ============================================================
st.title("SmartSheets Editor")
st.markdown(
    '<p style="color:#6F7585;margin-top:-12px;">'
    "Making SmartSheets smart again, multiple sheets at a time. Your welcome!"
    "</p>",
    unsafe_allow_html=True,
)

# ---- Validation gates ----
if not api_token:
    st.info("Enter your Smartsheet API token in the sidebar to begin.")
    st.stop()

if not workspace_id.strip():
    st.info("Enter your project Workspace ID in the sidebar.")
    st.stop()

try:
    workspace_id_int = int(workspace_id.strip())
except ValueError:
    st.error("Workspace ID must be a number.")
    st.stop()

instance_columns = [c.strip() for c in instance_columns_text.split("\n") if c.strip()]

# ============================================================
# Tabs
# ============================================================
tab2, tab1, tab3 = st.tabs(["Row Editor", "Checklist Sync", "Copy Workspace"])


# ============================================================
# Tab 1 -- Checklist Sync (existing scan / apply workflow)
# ============================================================
with tab1:

    st.divider()

    col_a, col_b = st.columns([1, 4])
    with col_a:
        scan_clicked = st.button(
            "Scan workspace",
            type="primary",
            use_container_width=True,
            disabled=st.session_state.confirm_apply,
        )
    with col_b:
        if st.session_state.scan_timestamp:
            st.markdown(
                '<div class="scan-meta">Last scan: '
                + st.session_state.scan_timestamp.strftime("%Y-%m-%d %H:%M:%S")
                + "</div>",
                unsafe_allow_html=True,
            )

    st.caption(
        "Reads every master sheet and compares it to the matching sheet in each generator folder. "
        "No changes are written."
    )

    if scan_clicked:
        reset_results()
        try:
            with st.spinner("Reading workspace..."):
                client = smartsheet.Smartsheet(api_token)
                client.errors_as_exceptions(True)

                template_sheets, generators = sync.get_workspace_layout(
                    client, workspace_id_int, templates_folder.strip() or "Templates",
                )
                plans, warnings = sync.build_plans(
                    client, template_sheets, generators,
                    instance_columns,
                    key_column_input.strip() or None,
                    allow_empty_master=False,
                )

                st.session_state.plans = plans
                st.session_state.scan_data = {
                    "plans_serialized": [sync.plan_to_dict(p, i) for i, p in enumerate(plans)],
                    "warnings": warnings,
                    "master_count": len(template_sheets),
                    "generator_count": len(generators),
                    "generator_folders": [name for name, _ in generators],
                }
                st.session_state.scan_timestamp = datetime.now()
        except smartsheet.exceptions.ApiError as e:
            st.error(f"Smartsheet API error: {e}")
        except RuntimeError as e:
            st.error(str(e))
        except Exception as e:
            st.error(f"Scan failed: {type(e).__name__}: {e}")

    # ---- Render scan results ----
    if st.session_state.scan_data:
        data = st.session_state.scan_data
        plans_serialized = data["plans_serialized"]

        total_adds = sum(p["counts"]["add"] for p in plans_serialized)
        total_updates = sum(p["counts"]["update"] for p in plans_serialized)
        total_deletes = sum(p["counts"]["delete"] for p in plans_serialized)

        st.divider()
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Masters", data["master_count"])
        m2.metric("Generators", data["generator_count"])
        m3.metric("Add", f"+{total_adds}")
        m4.metric("Update", f"~{total_updates}")
        m5.metric("Delete", f"-{total_deletes}")

        if data["warnings"]:
            with st.expander(f"  {len(data['warnings'])} warning(s)"):
                for w in data["warnings"]:
                    st.markdown(f"- `{w}`")

        if not plans_serialized:
            st.success("Every generator sheet matches its master. Nothing to apply.")
        else:
            st.divider()
            st.subheader("Preview")

            groups: dict = {}
            for p in plans_serialized:
                groups.setdefault(p["generator_folder"], []).append(p)

            for folder_name, sheets_in_folder in groups.items():
                st.markdown(f"#### `{folder_name}`")
                for p in sheets_in_folder:
                    counts = p["counts"]
                    summary = f"`+{counts['add']}` `~{counts['update']}` `-{counts['delete']}`"
                    with st.expander(
                        f"**{p['generator_path']}**  .  {summary}",
                        expanded=(len(sheets_in_folder) == 1 and len(groups) == 1),
                    ):
                        st.caption(f"Key column: `{p['key_column']}`")

                        if p["rows_to_add"]:
                            st.markdown(f"**Add ({len(p['rows_to_add'])})**")
                            for r in p["rows_to_add"]:
                                key = r["key"] or "(no key)"
                                st.markdown(
                                    f'<div class="diff-add">+ {key}</div>',
                                    unsafe_allow_html=True,
                                )

                        if p["rows_to_update"]:
                            st.markdown(f"**Update ({len(p['rows_to_update'])})**")
                            for r in p["rows_to_update"]:
                                key = r["key"] or "(no key)"
                                st.markdown(
                                    f'<div class="diff-update">~ {key}</div>',
                                    unsafe_allow_html=True,
                                )
                                for d in r["diffs"]:
                                    old = d["old"] or "(empty)"
                                    new = d["new"] or "(empty)"
                                    st.markdown(
                                        f'<div class="diff-detail">'
                                        f'<span class="col">{d["column"]}:</span> '
                                        f'<span class="old">{old}</span> '
                                        f'<span class="new">{new}</span>'
                                        f"</div>",
                                        unsafe_allow_html=True,
                                    )

                        if p["rows_to_delete"]:
                            st.markdown(f"**Delete ({len(p['rows_to_delete'])})**")
                            for r in p["rows_to_delete"]:
                                key = r["key"] or "(no key)"
                                st.markdown(
                                    f'<div class="diff-delete">- {key}</div>',
                                    unsafe_allow_html=True,
                                )

            # ---- Apply controls ----
            st.divider()
            st.subheader("Apply")

            if not st.session_state.confirm_apply:
                st.markdown(
                    f"Ready to write **+{total_adds} adds / ~{total_updates} updates / "
                    f"-{total_deletes} deletes** across **{len(plans_serialized)} sheet(s)**. "
                    "Instance columns will not be touched."
                )
                if st.button("Apply changes", type="primary", use_container_width=False):
                    st.session_state.confirm_apply = True
                    st.rerun()
            else:
                st.warning(
                    "**Confirm:** This writes to Smartsheet. There is no undo. "
                    "Smartsheet keeps cell history, but bulk recovery is painful."
                )
                cc1, cc2, _ = st.columns([1, 1, 3])
                with cc1:
                    if st.button("Yes, apply", type="primary", use_container_width=True):
                        results = []
                        progress = st.progress(0.0, text="Applying...")
                        try:
                            client = smartsheet.Smartsheet(api_token)
                            client.errors_as_exceptions(True)
                            plans_list = st.session_state.plans
                            for i, plan in enumerate(plans_list):
                                label = (
                                    f"{plan.generator_folder} / "
                                    f"{'/'.join(plan.generator.rel_path)}"
                                )
                                try:
                                    sync.apply_plan(client, plan)
                                    results.append({"ok": True, "label": label})
                                except Exception as e:
                                    results.append({"ok": False, "label": label, "error": str(e)})
                                progress.progress(
                                    (i + 1) / len(plans_list),
                                    text=f"Applying... ({i + 1}/{len(plans_list)})",
                                )
                        finally:
                            progress.empty()
                        st.session_state.apply_result = results
                        st.session_state.plans = []
                        st.session_state.scan_data = None
                        st.session_state.confirm_apply = False
                        st.rerun()
                with cc2:
                    if st.button("Cancel", use_container_width=True):
                        st.session_state.confirm_apply = False
                        st.rerun()

    # ---- Apply results ----
    if st.session_state.apply_result:
        st.divider()
        st.subheader("Apply result")
        results = st.session_state.apply_result
        fails = [r for r in results if not r["ok"]]
        if not fails:
            st.success(f"Synced {len(results)} sheet(s) successfully.")
        elif len(fails) == len(results):
            st.error(f"All {len(results)} sheet(s) failed.")
        else:
            st.warning(f"{len(results) - len(fails)} succeeded / {len(fails)} failed.")

        for r in results:
            if r["ok"]:
                st.markdown(f"+ `{r['label']}`")
            else:
                st.markdown(f"x `{r['label']}`")
                st.code(r.get("error", ""), language=None)

        if st.button("Clear and run another scan"):
            reset_results()
            st.rerun()


# ============================================================
# Tab 2 -- Row Editor / Add Row
# ============================================================
with tab2:
    st.markdown(
        "Select folders and a sheet, then edit an existing row or append a new one "
        "across all selected folders."
    )

    # ---- Step 1: Load workspace folders & sheets ----
    col_load, _ = st.columns([1, 4])
    with col_load:
        load_ws_clicked = st.button(
            "Load Workspace",
            type="secondary",
            use_container_width=True,
            key="fr_load_ws",
        )

    if load_ws_clicked:
        try:
            with st.spinner("Loading folders and sheets..."):
                client = smartsheet.Smartsheet(api_token)
                client.errors_as_exceptions(True)
                folders = sync.list_workspace_folders(client, workspace_id_int)
                sheets_by_folder: dict = {}
                for fname, fid in folders:
                    sheets_by_folder[fname] = sync.list_sheets_in_folder(client, fid)
            st.session_state.fr_folders_list = folders
            st.session_state.fr_sheets_by_folder = sheets_by_folder
            st.session_state.fr_loaded_row = None
            st.session_state.fr_loaded_col_names = []
            st.session_state.fr_apply_result = None
            st.session_state.fr_add_col_names = []
            st.session_state.fr_add_col_name_to_id = {}
            st.session_state.fr_add_result = None
        except smartsheet.exceptions.ApiError as e:
            st.error(f"Smartsheet API error: {e}")
        except Exception as e:
            st.error(f"Failed to load workspace: {e}")

    folders_list = st.session_state.fr_folders_list

    if folders_list is None:
        st.info("Click **Load Workspace** to browse folders and sheets.")
    else:
        folder_names = [name for name, _ in folders_list]

        # ---- Step 2: Select folders ----
        selected_folders = st.multiselect(
            "Folders to apply changes to",
            options=folder_names,
            key="fr_selected_folders",
            help="Changes will be written to the matching sheet in every selected folder.",
        )

        if not selected_folders:
            st.info("Select one or more folders to continue.")
        else:
            sheets_by_folder = st.session_state.fr_sheets_by_folder

            sheet_paths: set = set()
            for fname in selected_folders:
                for sref in sheets_by_folder.get(fname, []):
                    sheet_paths.add("/".join(sref.rel_path))
            sheet_path_list = sorted(sheet_paths)

            if not sheet_path_list:
                st.warning("No sheets found in the selected folders.")
            else:
                # Reset sheet selection when options no longer include current value
                current_sheet = st.session_state.get("fr_selected_sheet_path", "")
                if (
                    current_sheet not in sheet_path_list
                    and "fr_selected_sheet_path" in st.session_state
                ):
                    del st.session_state["fr_selected_sheet_path"]

                # ---- Step 3: Select sheet ----
                st.selectbox(
                    "Sheet",
                    options=sheet_path_list,
                    key="fr_selected_sheet_path",
                    help="Must exist in all selected folders.",
                )
                selected_sheet_path = st.session_state.fr_selected_sheet_path

                # ---- Mode selector ----
                mode = st.radio(
                    "Action",
                    options=["Edit existing row", "Add new row", "Delete row"],
                    key="fr_mode",
                    horizontal=True,
                )

                st.divider()

                # ================================================
                # Mode A -- Edit existing row
                # ================================================
                if mode == "Edit existing row":
                    st.number_input(
                        "Row number",
                        min_value=1,
                        value=int(st.session_state.fr_row_number),
                        step=1,
                        key="fr_row_number_input",
                        help="1-based row position (row 1 = first data row).",
                    )

                    load_row_clicked = st.button(
                        "Load Row",
                        type="primary",
                        key="fr_load_row",
                    )

                    if load_row_clicked:
                        row_num = int(st.session_state.fr_row_number_input)
                        ref_sheet = None
                        ref_folder_name = None
                        for fname in selected_folders:
                            for sref in sheets_by_folder.get(fname, []):
                                if "/".join(sref.rel_path) == selected_sheet_path:
                                    ref_sheet = sref
                                    ref_folder_name = fname
                                    break
                            if ref_sheet:
                                break

                        if ref_sheet is None:
                            st.error("Selected sheet not found in any of the chosen folders.")
                        else:
                            try:
                                with st.spinner(
                                    f"Loading row {row_num} from "
                                    f"'{ref_folder_name}/{selected_sheet_path}'..."
                                ):
                                    client = smartsheet.Smartsheet(api_token)
                                    client.errors_as_exceptions(True)
                                    row_data, col_name_to_id, col_names_ordered, col_editable = (
                                        sync.fetch_row_by_number(
                                            client, ref_sheet.sheet_id, row_num
                                        )
                                    )
                                st.session_state.fr_loaded_row = row_data
                                st.session_state.fr_loaded_col_names = col_names_ordered
                                st.session_state.fr_col_name_to_id = col_name_to_id
                                st.session_state.fr_col_name_to_editable = col_editable
                                st.session_state.fr_row_number = row_num
                                st.session_state.fr_apply_result = None
                                for col_name in col_names_ordered:
                                    val = row_data.cells_by_col_name.get(col_name, "")
                                    st.session_state[f"fr_cell_{col_name}"] = (
                                        str(val) if val is not None else ""
                                    )
                            except ValueError as e:
                                st.error(str(e))
                            except smartsheet.exceptions.ApiError as e:
                                st.error(f"Smartsheet API error: {e}")
                            except Exception as e:
                                st.error(f"Failed to load row: {e}")

                    loaded_row = st.session_state.fr_loaded_row
                    col_names = st.session_state.fr_loaded_col_names

                    if loaded_row is not None:
                        st.subheader(
                            f"Row {st.session_state.fr_row_number} -- {selected_sheet_path}"
                        )
                        st.caption(
                            "Edit the values below. Leave a field blank to clear that cell. "
                            "System and formula columns are read-only and won't be changed."
                        )

                        col_editable_map = st.session_state.get("fr_col_name_to_editable", {})
                        edited_values: dict = {}
                        for col_name in col_names:
                            is_editable = col_editable_map.get(col_name, True)
                            edited_values[col_name] = st.text_input(
                                col_name if is_editable else f"{col_name} (read-only)",
                                key=f"fr_cell_{col_name}",
                                disabled=not is_editable,
                            )

                        st.divider()

                        apply_label = (
                            f"Apply to {len(selected_folders)} folder(s)"
                            if len(selected_folders) > 1
                            else f"Apply to '{selected_folders[0]}'"
                        )
                        apply_clicked = st.button(
                            apply_label,
                            type="primary",
                            key="fr_apply_row",
                        )

                        if apply_clicked:
                            results = []
                            try:
                                client = smartsheet.Smartsheet(api_token)
                                client.errors_as_exceptions(True)
                                progress = st.progress(0.0, text="Applying...")

                                for idx, fname in enumerate(selected_folders):
                                    target_sheet = None
                                    for sref in sheets_by_folder.get(fname, []):
                                        if "/".join(sref.rel_path) == selected_sheet_path:
                                            target_sheet = sref
                                            break

                                    if target_sheet is None:
                                        results.append({
                                            "ok": False,
                                            "folder": fname,
                                            "error": "Sheet not found in this folder.",
                                        })
                                        progress.progress((idx + 1) / len(selected_folders))
                                        continue

                                    try:
                                        target_row_data, target_col_name_to_id, _, _ = (
                                            sync.fetch_row_by_number(
                                                client,
                                                target_sheet.sheet_id,
                                                int(st.session_state.fr_row_number),
                                            )
                                        )
                                        _, _, target_col_name_to_type, target_col_editable = (
                                            sync.fetch_sheet_columns(
                                                client, target_sheet.sheet_id
                                            )
                                        )
                                        sync.update_row_cells(
                                            client,
                                            target_sheet.sheet_id,
                                            target_row_data.row_id,
                                            target_col_name_to_id,
                                            edited_values,
                                            target_col_name_to_type,
                                            target_col_editable,
                                        )
                                        results.append({"ok": True, "folder": fname})
                                    except Exception as e:
                                        results.append({
                                            "ok": False,
                                            "folder": fname,
                                            "error": str(e),
                                        })

                                    progress.progress((idx + 1) / len(selected_folders))

                                progress.empty()
                            except Exception as e:
                                st.error(f"Unexpected error: {e}")

                            st.session_state.fr_apply_result = results
                            st.rerun()

                        fr_apply_result = st.session_state.fr_apply_result
                        if fr_apply_result:
                            st.divider()
                            fails = [r for r in fr_apply_result if not r["ok"]]
                            row_num_done = st.session_state.fr_row_number
                            if not fails:
                                st.success(
                                    f"Row {row_num_done} updated in "
                                    f"{len(fr_apply_result)} folder(s)."
                                )
                            elif len(fails) == len(fr_apply_result):
                                st.error(f"All {len(fr_apply_result)} update(s) failed.")
                            else:
                                st.warning(
                                    f"{len(fr_apply_result) - len(fails)} succeeded / "
                                    f"{len(fails)} failed."
                                )
                            for r in fr_apply_result:
                                if r["ok"]:
                                    st.markdown(f"+ `{r['folder']}`")
                                else:
                                    st.markdown(
                                        f"x `{r['folder']}`: {r.get('error', 'Unknown error')}"
                                    )

                # ================================================
                # Mode B -- Add new row
                # ================================================
                elif mode == "Add new row":
                    st.caption(
                        "Load the sheet's column structure, fill in values for the new row, "
                        "choose where it should be inserted, then add it in every selected folder."
                    )

                    load_cols_clicked = st.button(
                        "Load Sheet Columns",
                        type="primary",
                        key="fr_load_cols",
                    )

                    if load_cols_clicked:
                        ref_sheet = None
                        ref_folder_name = None
                        for fname in selected_folders:
                            for sref in sheets_by_folder.get(fname, []):
                                if "/".join(sref.rel_path) == selected_sheet_path:
                                    ref_sheet = sref
                                    ref_folder_name = fname
                                    break
                            if ref_sheet:
                                break

                        if ref_sheet is None:
                            st.error("Selected sheet not found in any of the chosen folders.")
                        else:
                            try:
                                with st.spinner(
                                    f"Loading columns from "
                                    f"'{ref_folder_name}/{selected_sheet_path}'..."
                                ):
                                    client = smartsheet.Smartsheet(api_token)
                                    client.errors_as_exceptions(True)
                                    col_name_to_id, col_names_ordered, col_name_to_type, col_editable = (
                                        sync.fetch_sheet_columns(
                                            client, ref_sheet.sheet_id
                                        )
                                    )
                                st.session_state.fr_add_col_names = col_names_ordered
                                st.session_state.fr_add_col_name_to_id = col_name_to_id
                                st.session_state.fr_col_name_to_type = col_name_to_type
                                st.session_state.fr_add_col_name_to_editable = col_editable
                                st.session_state.fr_add_result = None
                                for col_name in col_names_ordered:
                                    st.session_state[f"fr_new_{col_name}"] = ""
                            except smartsheet.exceptions.ApiError as e:
                                st.error(f"Smartsheet API error: {e}")
                            except Exception as e:
                                st.error(f"Failed to load columns: {e}")

                    add_col_names = st.session_state.fr_add_col_names

                    if add_col_names:
                        st.subheader(f"New row -- {selected_sheet_path}")
                        st.caption(
                            "Fill in the values for the new row. Blank fields will be skipped. "
                            "System and formula columns are read-only and won't be set."
                        )

                        add_editable_map = st.session_state.get("fr_add_col_name_to_editable", {})
                        new_values: dict = {}
                        for col_name in add_col_names:
                            is_editable = add_editable_map.get(col_name, True)
                            new_values[col_name] = st.text_input(
                                col_name if is_editable else f"{col_name} (read-only)",
                                key=f"fr_new_{col_name}",
                                disabled=not is_editable,
                            )

                        st.divider()

                        st.subheader("Where should the row go?")
                        add_position = st.radio(
                            "Insert position",
                            options=[
                                "Bottom of sheet",
                                "Top of sheet",
                                "Above a specific row",
                                "Below a specific row",
                            ],
                            key="fr_add_position",
                        )

                        sibling_row_number = None
                        if add_position in ("Above a specific row", "Below a specific row"):
                            sibling_row_number = int(
                                st.number_input(
                                    "Row number",
                                    min_value=1,
                                    value=int(st.session_state.fr_add_sibling_row_number),
                                    step=1,
                                    key="fr_add_sibling_row_number",
                                    help=(
                                        "1-based row position. The new row will be inserted "
                                        f"{'above' if add_position.startswith('Above') else 'below'} "
                                        "this row in every selected folder."
                                    ),
                                )
                            )

                        position_map = {
                            "Bottom of sheet": "bottom",
                            "Top of sheet": "top",
                            "Above a specific row": "above",
                            "Below a specific row": "below",
                        }
                        position_arg = position_map[add_position]

                        st.divider()

                        add_label = (
                            f"Add row to {len(selected_folders)} folder(s)"
                            if len(selected_folders) > 1
                            else f"Add row to '{selected_folders[0]}'"
                        )
                        add_clicked = st.button(
                            add_label,
                            type="primary",
                            key="fr_add_row",
                        )

                        if add_clicked:
                            results = []
                            try:
                                client = smartsheet.Smartsheet(api_token)
                                client.errors_as_exceptions(True)
                                progress = st.progress(0.0, text="Adding rows...")

                                for idx, fname in enumerate(selected_folders):
                                    target_sheet = None
                                    for sref in sheets_by_folder.get(fname, []):
                                        if "/".join(sref.rel_path) == selected_sheet_path:
                                            target_sheet = sref
                                            break

                                    if target_sheet is None:
                                        results.append({
                                            "ok": False,
                                            "folder": fname,
                                            "error": "Sheet not found in this folder.",
                                        })
                                        progress.progress((idx + 1) / len(selected_folders))
                                        continue

                                    try:
                                        (
                                            target_col_name_to_id,
                                            _,
                                            target_col_name_to_type,
                                            target_col_editable,
                                        ) = sync.fetch_sheet_columns(
                                            client, target_sheet.sheet_id
                                        )
                                        sync.add_row_to_sheet(
                                            client,
                                            target_sheet.sheet_id,
                                            target_col_name_to_id,
                                            new_values,
                                            target_col_name_to_type,
                                            target_col_editable,
                                            position=position_arg,
                                            sibling_row_number=sibling_row_number,
                                        )
                                        results.append({"ok": True, "folder": fname})
                                    except Exception as e:
                                        results.append({
                                            "ok": False,
                                            "folder": fname,
                                            "error": str(e),
                                        })

                                    progress.progress((idx + 1) / len(selected_folders))

                                progress.empty()
                            except Exception as e:
                                st.error(f"Unexpected error: {e}")

                            st.session_state.fr_add_result = results
                            st.rerun()

                    fr_add_result = st.session_state.fr_add_result
                    if fr_add_result:
                        st.divider()
                        fails = [r for r in fr_add_result if not r["ok"]]
                        if not fails:
                            st.success(f"New row added to {len(fr_add_result)} folder(s).")
                        elif len(fails) == len(fr_add_result):
                            st.error(f"All {len(fr_add_result)} add(s) failed.")
                        else:
                            st.warning(
                                f"{len(fr_add_result) - len(fails)} succeeded / "
                                f"{len(fails)} failed."
                            )
                        for r in fr_add_result:
                            if r["ok"]:
                                st.markdown(f"+ `{r['folder']}`")
                            else:
                                st.markdown(
                                    f"x `{r['folder']}`: {r.get('error', 'Unknown error')}"
                                )

                # ================================================
                # Mode C -- Delete row
                # ================================================
                elif mode == "Delete row":
                    st.number_input(
                        "Row number",
                        min_value=1,
                        value=int(st.session_state.fr_row_number),
                        step=1,
                        key="fr_del_row_number_input",
                        help="1-based row position of the row to delete.",
                    )

                    load_del_row_clicked = st.button(
                        "Load Row",
                        type="secondary",
                        key="fr_load_del_row",
                    )

                    if load_del_row_clicked:
                        row_num = int(st.session_state.fr_del_row_number_input)
                        ref_sheet = None
                        ref_folder_name = None
                        for fname in selected_folders:
                            for sref in sheets_by_folder.get(fname, []):
                                if "/".join(sref.rel_path) == selected_sheet_path:
                                    ref_sheet = sref
                                    ref_folder_name = fname
                                    break
                            if ref_sheet:
                                break

                        if ref_sheet is None:
                            st.error("Selected sheet not found in any of the chosen folders.")
                        else:
                            try:
                                with st.spinner(
                                    f"Loading row {row_num} from "
                                    f"'{ref_folder_name}/{selected_sheet_path}'..."
                                ):
                                    client = smartsheet.Smartsheet(api_token)
                                    client.errors_as_exceptions(True)
                                    row_data, col_name_to_id, col_names_ordered, _ = (
                                        sync.fetch_row_by_number(
                                            client, ref_sheet.sheet_id, row_num
                                        )
                                    )
                                st.session_state.fr_del_loaded_row = row_data
                                st.session_state.fr_del_col_names = col_names_ordered
                                st.session_state.fr_del_row_number = row_num
                                st.session_state.fr_del_confirm = False
                                st.session_state.fr_del_result = None
                            except ValueError as e:
                                st.error(str(e))
                            except smartsheet.exceptions.ApiError as e:
                                st.error(f"Smartsheet API error: {e}")
                            except Exception as e:
                                st.error(f"Failed to load row: {e}")

                    del_loaded_row = st.session_state.get("fr_del_loaded_row")
                    del_col_names = st.session_state.get("fr_del_col_names", [])

                    if del_loaded_row is not None:
                        row_num_del = st.session_state.get("fr_del_row_number", "?")
                        st.subheader(f"Row {row_num_del} -- {selected_sheet_path}")
                        st.caption("This row will be deleted from every selected folder. Review its contents below.")

                        for col_name in del_col_names:
                            val = del_loaded_row.cells_by_col_name.get(col_name, "")
                            st.text_input(
                                col_name,
                                value=str(val) if val is not None else "",
                                disabled=True,
                                key=f"fr_del_preview_{col_name}",
                            )

                        st.divider()
                        st.warning(
                            f"Deleting row {row_num_del} from **{len(selected_folders)} folder(s)**. "
                            "This cannot be undone (Smartsheet keeps row history but bulk recovery is painful)."
                        )

                        del_label = (
                            f"Delete row {row_num_del} from {len(selected_folders)} folder(s)"
                            if len(selected_folders) > 1
                            else f"Delete row {row_num_del} from '{selected_folders[0]}'"
                        )
                        del_clicked = st.button(
                            del_label,
                            type="primary",
                            key="fr_delete_row",
                        )

                        if del_clicked:
                            results = []
                            try:
                                client = smartsheet.Smartsheet(api_token)
                                client.errors_as_exceptions(True)
                                progress = st.progress(0.0, text="Deleting...")

                                for idx, fname in enumerate(selected_folders):
                                    target_sheet = None
                                    for sref in sheets_by_folder.get(fname, []):
                                        if "/".join(sref.rel_path) == selected_sheet_path:
                                            target_sheet = sref
                                            break

                                    if target_sheet is None:
                                        results.append({
                                            "ok": False,
                                            "folder": fname,
                                            "error": "Sheet not found in this folder.",
                                        })
                                        progress.progress((idx + 1) / len(selected_folders))
                                        continue

                                    try:
                                        sync.delete_row_by_number(
                                            client,
                                            target_sheet.sheet_id,
                                            int(st.session_state.fr_del_row_number),
                                        )
                                        results.append({"ok": True, "folder": fname})
                                    except Exception as e:
                                        results.append({
                                            "ok": False,
                                            "folder": fname,
                                            "error": str(e),
                                        })

                                    progress.progress((idx + 1) / len(selected_folders))

                                progress.empty()
                            except Exception as e:
                                st.error(f"Unexpected error: {e}")

                            st.session_state.fr_del_result = results
                            st.session_state.fr_del_loaded_row = None
                            st.session_state.fr_del_col_names = []
                            st.rerun()

                    fr_del_result = st.session_state.get("fr_del_result")
                    if fr_del_result:
                        st.divider()
                        fails = [r for r in fr_del_result if not r["ok"]]
                        row_num_done = st.session_state.get("fr_del_row_number", "?")
                        if not fails:
                            st.success(f"Row {row_num_done} deleted from {len(fr_del_result)} folder(s).")
                        elif len(fails) == len(fr_del_result):
                            st.error(f"All {len(fr_del_result)} delete(s) failed.")
                        else:
                            st.warning(
                                f"{len(fr_del_result) - len(fails)} succeeded / "
                                f"{len(fails)} failed."
                            )
                        for r in fr_del_result:
                            if r["ok"]:
                                st.markdown(f"+ `{r['folder']}`")
                            else:
                                st.markdown(
                                    f"x `{r['folder']}`: {r.get('error', 'Unknown error')}"
                                )


# ============================================================
# Tab 3 -- Copy Workspace (large-workspace, folder-by-folder)
# ============================================================
with tab3:
    st.markdown(
        "Smartsheet refuses to copy a workspace once it grows past its item limit. "
        "This copies it **folder by folder** into a brand-new workspace instead, so "
        "large projects can be duplicated."
    )
    st.warning(
        "Because the copy runs folder by folder, **cell links, cross-sheet "
        "references, and dashboards that point _across_ folders are not re-linked** "
        "to the new copy -- they keep pointing at the original. Everything inside a "
        "single folder is copied and re-linked correctly."
    )

    cw_src = st.text_input(
        "Source workspace ID",
        value=str(workspace_id).strip(),
        key="cw_src",
        help="Defaults to the workspace ID from the sidebar. Override to copy a different one.",
    )
    cw_name = st.text_input(
        "New workspace name",
        key="cw_name",
        placeholder="e.g. Stage 6 (copy)",
    )
    cw_include_labels = st.multiselect(
        "Include",
        options=list(sync.COPY_INCLUDE_OPTIONS.keys()),
        default=sync.COPY_INCLUDE_DEFAULT_LABELS,
        key="cw_include",
        help="What to carry over into the copy. 'Sharing' also copies who the sheets are shared with.",
    )

    def _cw_src_int():
        try:
            return int(str(cw_src).strip())
        except (ValueError, TypeError):
            return None

    # ---- Preview ----
    col_p, col_c = st.columns(2)
    with col_p:
        preview_clicked = st.button(
            "Preview source", use_container_width=True, key="cw_preview_btn"
        )
    with col_c:
        copy_clicked = st.button(
            "Copy workspace",
            type="primary",
            use_container_width=True,
            key="cw_copy_btn",
            disabled=st.session_state.cw_confirm,
        )

    if preview_clicked:
        st.session_state.cw_confirm = False
        st.session_state.cw_report = None
        src_id = _cw_src_int()
        if src_id is None:
            st.error("Source workspace ID must be a number.")
        else:
            try:
                with st.spinner("Inspecting source workspace..."):
                    client = smartsheet.Smartsheet(api_token)
                    client.errors_as_exceptions(True)
                    st.session_state.cw_preview = sync.summarize_workspace(client, src_id)
            except smartsheet.exceptions.ApiError as e:
                st.error(f"Smartsheet API error: {e}")
            except Exception as e:
                st.error(f"Preview failed: {type(e).__name__}: {e}")

    if st.session_state.cw_preview:
        prev = st.session_state.cw_preview
        st.divider()
        st.markdown(f"**Source:** `{prev['workspace_name']}`")
        c1, c2, c3 = st.columns(3)
        c1.metric("Top-level folders", len(prev["folders"]))
        c2.metric("Sheets (total)", prev["total_sheets"])
        c3.metric("Top-level sheets", len(prev["top_level_sheets"]))
        if prev["folders"]:
            with st.expander("Folders to copy", expanded=False):
                for fname, fcount in prev["folders"]:
                    st.markdown(f"- `{fname}` — {fcount} sheet(s)")
        if prev["top_level_sights"] or prev["top_level_reports"]:
            st.info(
                f"{prev['top_level_sights']} dashboard(s) and {prev['top_level_reports']} "
                "report(s) sit at the workspace root and will NOT be copied. "
                "(Dashboards/reports inside folders are copied normally.)"
            )

    # ---- Two-step confirm ----
    if copy_clicked:
        src_id = _cw_src_int()
        if src_id is None:
            st.error("Source workspace ID must be a number.")
        elif not str(cw_name).strip():
            st.error("Enter a name for the new workspace.")
        else:
            st.session_state.cw_confirm = True

    if st.session_state.cw_confirm:
        st.warning(
            f"This will create a new workspace named **{str(cw_name).strip()}** and copy "
            "the source into it. This can take a while for large workspaces."
        )
        cc1, cc2 = st.columns(2)
        with cc1:
            confirm_go = st.button(
                "Confirm copy", type="primary", use_container_width=True, key="cw_confirm_btn"
            )
        with cc2:
            if st.button("Cancel", use_container_width=True, key="cw_cancel_btn"):
                st.session_state.cw_confirm = False
                st.rerun()

        if confirm_go:
            st.session_state.cw_confirm = False
            src_id = _cw_src_int()
            log_box = st.container()
            lines: list[str] = []

            def _progress(msg: str):
                lines.append(msg)
                with log_box:
                    st.write(msg)

            try:
                with st.status("Copying workspace...", expanded=True):
                    client = smartsheet.Smartsheet(api_token)
                    client.errors_as_exceptions(True)
                    report = sync.copy_workspace_piecewise(
                        client,
                        src_id,
                        str(cw_name).strip(),
                        include_labels=cw_include_labels,
                        progress=_progress,
                    )
                st.session_state.cw_report = report
            except smartsheet.exceptions.ApiError as e:
                st.error(f"Smartsheet API error: {e}")
            except Exception as e:
                st.error(f"Copy failed: {type(e).__name__}: {e}")

    # ---- Report ----
    report = st.session_state.cw_report
    if report:
        st.divider()
        if report.failed:
            st.warning(
                f"Copied {len(report.copied)} item(s), but {len(report.failed)} failed."
            )
        else:
            st.success(f"Copied {len(report.copied)} item(s) successfully.")

        if report.permalink:
            st.markdown(f"**Open the new workspace:** [{report.new_workspace_name}]({report.permalink})")
        elif report.new_workspace_id:
            st.markdown(f"New workspace ID: `{report.new_workspace_id}`")

        if report.failed:
            with st.expander(f"{len(report.failed)} failure(s)", expanded=True):
                for item, reason in report.failed:
                    st.markdown(f"- `{item}` — {reason}")
        if report.warnings:
            with st.expander(f"{len(report.warnings)} warning(s)"):
                for w in report.warnings:
                    st.markdown(f"- {w}")
        with st.expander(f"{len(report.copied)} item(s) copied"):
            for item in report.copied:
                st.markdown(f"- `{item}`")
