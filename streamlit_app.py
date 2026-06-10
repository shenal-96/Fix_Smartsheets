"""
streamlit_app.py — Streamlit UI for Smartsheet Checklist Sync.

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


# ============================================================
# Page config
# ============================================================
st.set_page_config(
    page_title="Checklist Sync",
    page_icon="⊟",
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


def init_state() -> None:
    defaults = {
        "plans": [],               # list of SheetPlan from last scan
        "scan_data": None,         # serialized form for display
        "scan_timestamp": None,
        "confirm_apply": False,
        "apply_result": None,
        # Pre-populate config fields from secrets if available
        "cfg_workspace_id": str(get_secret("DEFAULT_WORKSPACE_ID", "")),
        "cfg_templates_folder": "Templates",
        "cfg_instance_columns": "Check Status\nSigned Off By\nDate Completed\nComments",
        "cfg_key_column": "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def reset_results() -> None:
    st.session_state.plans = []
    st.session_state.scan_data = None
    st.session_state.scan_timestamp = None
    st.session_state.confirm_apply = False
    st.session_state.apply_result = None


init_state()


# ============================================================
# Custom CSS — a few tweaks to elevate the default look
# ============================================================
st.markdown(
    """
    <style>
    /* Cream-ish background tone */
    .stApp { background-color: #F6F2E8; }

    /* Tighter heading sizes, more editorial */
    h1 { font-weight: 600 !important; letter-spacing: -0.02em; }
    h2, h3 { letter-spacing: -0.01em; }

    /* Sidebar background */
    section[data-testid="stSidebar"] { background-color: #FBF8F0; border-right: 1px solid #E0D8C2; }

    /* Metrics: tighter look */
    div[data-testid="stMetricValue"] { font-size: 1.9rem; font-weight: 600; }
    div[data-testid="stMetricLabel"] { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; }

    /* Diff-style monospace blocks */
    .diff-add    { background: #E9F0E9; color: #2F6A4E; padding: 4px 8px; border-left: 3px solid #2F6A4E; font-family: monospace; font-size: 0.875rem; margin: 2px 0; border-radius: 3px; }
    .diff-update { background: #F4ECD8; color: #9C6A2E; padding: 4px 8px; border-left: 3px solid #9C6A2E; font-family: monospace; font-size: 0.875rem; margin: 2px 0; border-radius: 3px; }
    .diff-delete { background: #F1DDDF; color: #97384A; padding: 4px 8px; border-left: 3px solid #97384A; font-family: monospace; font-size: 0.875rem; margin: 2px 0; border-radius: 3px; }
    .diff-detail { font-family: monospace; font-size: 0.8rem; color: #3F4655; margin: 2px 0 6px 18px; }
    .diff-detail .old { color: #97384A; text-decoration: line-through; }
    .diff-detail .new { color: #2F6A4E; }
    .diff-detail .col { color: #6F7585; }

    /* Buttons */
    div.stButton > button[kind="primary"] { background-color: #234E7A; border-color: #234E7A; }
    div.stButton > button[kind="primary"]:hover { background-color: #16365A; border-color: #16365A; }

    /* Smaller "Last scan" caption */
    .scan-meta { color: #6F7585; font-family: monospace; font-size: 0.8rem; padding-top: 12px; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# Sidebar — settings
# ============================================================
with st.sidebar:
    st.markdown("## Checklist Sync")
    st.caption("Smartsheet master → generator copies")
    st.divider()

    st.markdown("### Connection")
    api_token = st.text_input(
        "Smartsheet API token",
        type="password",
        value=get_secret("SMARTSHEET_API_TOKEN", ""),
        help="Generate in Smartsheet: Account → Personal Settings → API Access. "
             "Not stored — entered each session.",
    )

    st.markdown("### Project")
    workspace_id = st.text_input(
        "Workspace ID",
        key="cfg_workspace_id",
        help="The project workspace ID. Right-click workspace in Smartsheet → Properties.",
    )

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
            Edit the master in `Templates/` → run a scan → apply.
            Instance columns (status, signoff, date, comments) are never touched.

            See the README in the GitHub repo for setup details.
            """
        )


# ============================================================
# Main area — header
# ============================================================
st.title("Checklist Sync")
st.markdown(
    '<p style="color:#6F7585;margin-top:-12px;">'
    "Propagate master checklist changes across every generator in a Smartsheet project."
    "</p>",
    unsafe_allow_html=True,
)

# ---- Validation gates ----
if not api_token:
    st.info("👈 Enter your Smartsheet API token in the sidebar to begin.")
    st.stop()

if not workspace_id.strip():
    st.info("👈 Enter your project Workspace ID in the sidebar.")
    st.stop()

try:
    workspace_id_int = int(workspace_id.strip())
except ValueError:
    st.error("Workspace ID must be a number.")
    st.stop()

instance_columns = [c.strip() for c in instance_columns_text.split("\n") if c.strip()]


# ============================================================
# Scan
# ============================================================
st.divider()

col_a, col_b = st.columns([1, 4])
with col_a:
    scan_clicked = st.button(
        "🔍 Scan workspace",
        type="primary",
        use_container_width=True,
        disabled=st.session_state.confirm_apply,
    )
with col_b:
    if st.session_state.scan_timestamp:
        st.markdown(
            f'<div class="scan-meta">Last scan: '
            f'{st.session_state.scan_timestamp.strftime("%Y-%m-%d %H:%M:%S")}</div>',
            unsafe_allow_html=True,
        )

st.caption("Reads every master sheet and compares it to the matching sheet in each generator folder. No changes are written.")

if scan_clicked:
    reset_results()
    try:
        with st.spinner("Reading workspace…"):
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


# ============================================================
# Render scan results
# ============================================================
if st.session_state.scan_data:
    data = st.session_state.scan_data
    plans_serialized = data["plans_serialized"]

    total_adds = sum(p["counts"]["add"] for p in plans_serialized)
    total_updates = sum(p["counts"]["update"] for p in plans_serialized)
    total_deletes = sum(p["counts"]["delete"] for p in plans_serialized)
    total_ops = total_adds + total_updates + total_deletes

    # ----- Stats row -----
    st.divider()
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Masters", data["master_count"])
    m2.metric("Generators", data["generator_count"])
    m3.metric("Add", f"+{total_adds}")
    m4.metric("Update", f"~{total_updates}")
    m5.metric("Delete", f"−{total_deletes}")

    # ----- Warnings -----
    if data["warnings"]:
        with st.expander(f"⚠️ {len(data['warnings'])} warning(s)"):
            for w in data["warnings"]:
                st.markdown(f"- `{w}`")

    # ----- Plans -----
    if not plans_serialized:
        st.success("✅ Every generator sheet matches its master. Nothing to apply.")
    else:
        st.divider()
        st.subheader("Preview")

        # Group by generator folder for visual organisation
        groups: dict = {}
        for p in plans_serialized:
            groups.setdefault(p["generator_folder"], []).append(p)

        for folder_name, sheets_in_folder in groups.items():
            st.markdown(f"#### `{folder_name}`")
            for p in sheets_in_folder:
                counts = p["counts"]
                summary_pills = (
                    f"`+{counts['add']}` `~{counts['update']}` `−{counts['delete']}`"
                )
                with st.expander(
                    f"**{p['generator_path']}**  ·  {summary_pills}",
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
                                old = d["old"] or "∅"
                                new = d["new"] or "∅"
                                st.markdown(
                                    f'<div class="diff-detail">'
                                    f'<span class="col">{d["column"]}:</span> '
                                    f'<span class="old">{old}</span> → '
                                    f'<span class="new">{new}</span>'
                                    f'</div>',
                                    unsafe_allow_html=True,
                                )

                    if p["rows_to_delete"]:
                        st.markdown(f"**Delete ({len(p['rows_to_delete'])})**")
                        for r in p["rows_to_delete"]:
                            key = r["key"] or "(no key)"
                            st.markdown(
                                f'<div class="diff-delete">− {key}</div>',
                                unsafe_allow_html=True,
                            )

        # ----- Apply controls -----
        st.divider()
        st.subheader("Apply")

        if not st.session_state.confirm_apply:
            st.markdown(
                f"Ready to write **+{total_adds} adds · ~{total_updates} updates · "
                f"−{total_deletes} deletes** across **{len(plans_serialized)} sheet(s)**. "
                "Instance columns will not be touched."
            )
            if st.button(
                "Apply changes",
                type="primary",
                use_container_width=False,
            ):
                st.session_state.confirm_apply = True
                st.rerun()
        else:
            st.warning(
                "**Confirm:** This writes to Smartsheet. There is no undo. "
                "Smartsheet keeps cell history, but bulk recovery is painful."
            )
            cc1, cc2, _ = st.columns([1, 1, 3])
            with cc1:
                if st.button("✓ Yes, apply", type="primary", use_container_width=True):
                    results = []
                    progress = st.progress(0.0, text="Applying…")
                    try:
                        client = smartsheet.Smartsheet(api_token)
                        client.errors_as_exceptions(True)
                        plans_list = st.session_state.plans
                        for i, plan in enumerate(plans_list):
                            label = f"{plan.generator_folder} / {'/'.join(plan.generator.rel_path)}"
                            try:
                                sync.apply_plan(client, plan)
                                results.append({"ok": True, "label": label})
                            except Exception as e:
                                results.append({"ok": False, "label": label, "error": str(e)})
                            progress.progress((i + 1) / len(plans_list), text=f"Applying… ({i + 1}/{len(plans_list)})")
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


# ============================================================
# Apply results (after a successful apply)
# ============================================================
if st.session_state.apply_result:
    st.divider()
    st.subheader("Apply result")
    results = st.session_state.apply_result
    fails = [r for r in results if not r["ok"]]
    if not fails:
        st.success(f"✅ Synced {len(results)} sheet(s) successfully.")
    elif len(fails) == len(results):
        st.error(f"❌ All {len(results)} sheet(s) failed.")
    else:
        st.warning(f"⚠️ {len(results) - len(fails)} succeeded · {len(fails)} failed.")

    for r in results:
        if r["ok"]:
            st.markdown(f"✓ `{r['label']}`")
        else:
            st.markdown(f"✗ `{r['label']}`")
            st.code(r.get("error", ""), language=None)

    if st.button("Clear and run another scan"):
        reset_results()
        st.rerun()
