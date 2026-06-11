# Checklist Sync — Smartsheet

A small web app that syncs checklist rows from a master template to every generator copy in a Smartsheet project. Edit one sheet, push to all generators in seconds. Per-generator data (check status, signoff, date, comments) is **never** touched.

Three ways to run it:

| Mode | When to use | Install needed |
|---|---|---|
| **Streamlit Cloud (hosted)** | Work machines, no admin rights, team access | None — just a browser |
| **Local Flask app** | Personal machine, can install Python | Python + Flask |
| **CLI** | Terminal users, scripting, automation | Python |

All three share the same core sync logic, the same safety guarantees, and the same data model.

---

## Quick start — Deploy to Streamlit Community Cloud (no local install)

This gets you a live, shareable URL at `https://<your-app-name>.streamlit.app/` for free.

### Step 1 — Get the code onto GitHub

You don't need git installed. You can do this entirely from your browser.

1. **Download the project zip** from wherever you received it and unzip it locally. (Windows Explorer and macOS Finder both unzip without extra software.)
2. Go to [github.com](https://github.com) and sign in (create a free account if you don't have one).
3. Click the **+** in the top right → **New repository**.
4. Name it (e.g. `checklist-sync`). Set it to **Private** if you'd prefer — Streamlit Community Cloud supports private repos for free.
5. **Don't** initialize with a README — leave the repo empty. Click **Create repository**.
6. On the empty repo page, click the link that says **"uploading an existing file"**.
7. Drag every file and folder from the unzipped project into the upload area:
   - `streamlit_app.py`
   - `smartsheet_sync.py`
   - `requirements.txt`
   - `README.md`
   - `.gitignore`
   - The `.streamlit/` folder
   - The other files (`app.py`, `sync_checklist.py`, `templates/`, `config.example.json`) — these aren't used by the Streamlit deployment but useful to keep in the repo for the CLI/Flask modes.
   - **Don't upload `config.json`** if you happen to have created one — it can contain your workspace ID. The `.gitignore` would normally exclude it locally but the web UI doesn't honour `.gitignore` so just don't drag it.
8. Scroll down. In the commit message box: "Initial commit". Click **Commit changes**.

You should now see all your files in the repo.

### Step 2 — Deploy on Streamlit Community Cloud

1. Go to [share.streamlit.io](https://share.streamlit.io).
2. Click **Sign in** → **Continue with GitHub**. Authorize Streamlit to access your repositories. (For private repos you'll need to explicitly grant access.)
3. Click **Create app** → **Deploy a public app from GitHub**.
4. Fill in the form:
   - **Repository:** `your-username/checklist-sync`
   - **Branch:** `main`
   - **Main file path:** `streamlit_app.py`
   - **App URL:** pick something memorable, e.g. `checklist-sync`
5. Click **Deploy**.

Watch the deployment log. First deploy takes 2–3 minutes (it builds a container, installs dependencies, etc.). When it finishes you'll be redirected to your live app.

### Step 3 — Use it

1. In the sidebar, paste your Smartsheet API token (Smartsheet → Account → Personal Settings → API Access → generate a new token).
2. Click **Scan workspaces** to list every workspace your API token can access, then pick the one you want by name from the dropdown (type in the box to search). You can still paste a workspace ID manually if you prefer (right-click workspace in Smartsheet → Properties).
3. The default instance columns are pre-loaded — adjust if your column names are different.
4. Click **Scan workspace**. You'll see the preview.
5. Review. If it looks right, click **Apply changes** → confirm.

### Optional: pre-load your token via Streamlit secrets

So you don't have to paste your token every session:

1. On your app's page in Streamlit Cloud, click **Settings** (bottom-right) → **Secrets**.
2. Paste:

   ```toml
   SMARTSHEET_API_TOKEN = "your_actual_token_here"
   DEFAULT_WORKSPACE_ID = "1234567890123456"
   ```

3. Save. The app reloads. Now your sidebar fields are pre-populated.

**Security note:** secrets stored in Streamlit Cloud are encrypted at rest and not visible in the repo or app logs. But the app itself is reachable by anyone with the URL. If you've pre-loaded your token in secrets, that means anyone who hits your app URL can use your token to modify your Smartsheet workspace.

**Recommended patterns:**
- **Personal use only, share URL with no one:** Pre-load in secrets is fine.
- **Anyone else might find the URL:** Don't pre-load the token. Make every user enter their own. Or make the repo private and use Streamlit's app-level password (see below).

### Optional: lock the app with a password

Streamlit doesn't have a built-in auth screen, but you can add a simple password gate. Edit `streamlit_app.py` to add at the very top (after the imports):

```python
def check_password():
    expected = get_secret("APP_PASSWORD", "")
    if not expected:
        return True  # no password configured, allow access
    if st.session_state.get("password_ok"):
        return True
    pw = st.text_input("App password", type="password")
    if pw and pw == expected:
        st.session_state.password_ok = True
        st.rerun()
    elif pw:
        st.error("Wrong password.")
    return False

if not check_password():
    st.stop()
```

Then in Streamlit Cloud → Settings → Secrets, add:

```toml
APP_PASSWORD = "pick_something_strong"
```

---

## How it works

The tool expects this folder layout in your project workspace:

```
Project Workspace (e.g. "Stage 6")
├── Templates/                            ← master sheets live here
│   ├── 0_Upfit Checks/
│   │   ├── 1.01 Component Serial Number Details
│   │   ├── 1.02 Generator Assembly/Upfit
│   │   └── Punch List
│   └── 1_Generator SAT ITP/
│       └── ...
├── 95031502610 - MDS/                    ← generator folders (one per gen)
│   ├── 0_Upfit Checks/                   (same sheet names + structure)
│   ├── 1_Generator SAT ITP/
└── 95031502611 - MDS/
    └── ...
```

**Rules:**

- Top-level folders other than `Templates` are treated as generator folders.
- For each master sheet, the tool finds a sheet at the **same relative path** inside each generator folder.
- Rows are matched by the value of the **primary column** (e.g. `1.02.3 Coolant level check`). Override with the "Key column" field if needed.
- Columns listed as "instance columns" are **never written**. Every other column is treated as a template column and synced.

**Diff actions per row:**

| Action | Trigger |
|---|---|
| Add    | Row in master, missing from generator → added to bottom |
| Update | Row in both, template column value differs → template cols updated, instance cols untouched |
| Delete | Row in generator, not in master → deleted |
| Skip   | Generator row with empty key → left alone, never deleted |

---

## Copy a large workspace

Smartsheet's built-in "Save as New" / copy-workspace refuses to copy a workspace once it exceeds its item limit (you'll have hit this around 100+ sheets). The **Copy Workspace** tab works around that by copying the workspace **folder by folder** into a brand-new workspace:

1. Enter the **source workspace ID** (defaults to the one in the sidebar) and a **name for the new workspace**.
2. Pick what to **include** (row data, attachments, comments, forms, automation rules, cell links, sharing).
3. Click **Preview source** to see the folders and sheet counts that will be copied.
4. Click **Copy workspace** → **Confirm**. Progress streams live; a report lists what was copied, what failed, and any warnings, plus a link to the new workspace.

If a *single* folder is itself over the limit, the tool automatically splits it — recreating the folder and copying its children one at a time.

**Important limitation:** because the copy runs folder by folder, **cell links, cross-sheet references, and dashboards that point _across_ folders are not re-linked** to the new copy — they keep pointing at the original workspace. Everything inside a single folder is copied and re-linked correctly. Dashboards and reports sitting at the workspace root (not inside a folder) are not copied and must be recreated manually.

---

## Safety features

- **Scan-then-Apply.** Scan only reads; nothing writes until you click Apply.
- **Two-step confirm.** Apply requires an explicit second click to confirm.
- **Empty-master protection.** If a master sheet has zero rows, sync refuses (otherwise it'd delete all rows in every copy).
- **Empty-key generator rows are never deleted.** Extra rows lacking a key value are left alone.
- **Missing columns are warned, not invented.** Column structure must already match.
- **Plans drop after apply.** You can't accidentally re-apply the same changes; you have to scan again.
- **No local config persistence on Streamlit Cloud.** Settings live in your session only. Closing the tab forgets them.

---

## What it does NOT do

- **Doesn't add/remove whole sheets**, only rows within sheets that exist in both Templates and a generator.
- **Doesn't add/remove/rename columns.** Column structure must match before you sync.
- **Doesn't preserve row order.** New rows are appended at the bottom of the generator.
- **Doesn't sync formulas, attachments, conditional formatting, automations.** Cell values only.
- **No undo.** Smartsheet has cell-level history per sheet but bulk recovery is painful. Use the dry-run preview.

---

## Local Flask app (alternative)

If you can install Python on a machine, you can run the same tool locally with a richer custom UI:

```bash
pip install -r requirements.txt
export SMARTSHEET_API_TOKEN="your_token"
python app.py
```

Open `http://localhost:5000`. See the original UI in `templates/index.html` — it has a more bespoke look than the Streamlit version but requires local install.

---

## CLI (alternative)

For terminal users or scripted runs:

```bash
pip install -r requirements.txt
cp config.example.json config.json
# edit config.json — set workspace_id, adjust instance_columns
export SMARTSHEET_API_TOKEN="your_token"

# Dry run
python sync_checklist.py

# Apply with confirmation
python sync_checklist.py --apply

# Apply without prompting (for scripting)
python sync_checklist.py --apply --yes
```

---

## Troubleshooting

**Streamlit app says "Connecting..." for a long time** — Community Cloud apps go to sleep after inactivity. First load after sleep takes 30–60 seconds. Subsequent uses are fast.

**`No folder named 'Templates' found`** — top-level folder is missing or named differently. Either rename in Smartsheet or update the folder name in the sidebar.

**`No matching sheet for '.../X' in generator folder 'Y'`** — sheet missing or at a different path in Y. Create it or align the paths.

**`Generator '...' missing columns from master`** — add columns with matching names+types to the generator, or remove from the master.

**Rows aren't matching when I expect them to** — values in the key column don't match exactly. Check for trailing spaces, capitalisation, or item renumbering. If a key value changes, the tool treats the rename as delete + add.

**Rate limit errors** — Smartsheet allows 300 requests/minute. Large projects can hit this. Wait a minute and re-run, or split into smaller runs by emptying some master sheets temporarily.

**My GitHub repo doesn't show in Streamlit Cloud** — for private repos, you need to grant Streamlit explicit access. Go to GitHub → Settings → Applications → Streamlit → Configure → enable the repo. Then refresh Streamlit Cloud.

---

## File structure

```
checklist-sync/
├── streamlit_app.py            ← Streamlit UI (deployed)
├── smartsheet_sync.py          ← Core sync logic (shared)
├── app.py                      ← Flask UI (local-only alternative)
├── sync_checklist.py           ← CLI (alternative)
├── templates/
│   └── index.html              ← Flask UI template
├── .streamlit/
│   ├── config.toml             ← Streamlit theme
│   └── secrets.example.toml    ← Example secrets (not committed)
├── config.example.json         ← Example config for the CLI
├── requirements.txt
├── .gitignore
└── README.md
```

---

## Recommended workflow

1. Decide who owns the templates (one person per project is cleanest).
2. They make checklist changes in `Templates/` only.
3. Open the Streamlit app, click **Scan**, read the preview.
4. **Apply**. Spot-check one generator sheet to confirm.
5. Notify the team that a new revision is out.

Per-generator status, signoff, date, and comments are entered as normal by inspectors — the sync tool never touches those columns.
