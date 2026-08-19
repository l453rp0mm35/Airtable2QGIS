# Airtable2QGIS

A QGIS Processing script that imports records from an Airtable table or view directly into QGIS as a CSV-based layer — with secure, encrypted API key storage and support for automated, repeatable updates.

> **Note on how this was built:** This tool was vibe-coded with the help of Claude Sonnet 5 (Anthropic). The author is not a professional software developer; the code has been tested for the described workflow but has not undergone a formal security or code review. Use at your own discretion, and feel free to open an issue or pull request if you spot a problem.

---

## What it does

- Connects to the [Airtable Web API](https://airtable.com/developers/web/api/introduction) and fetches records from a specified base, table, and (optionally) a filtered view.
- Lets you choose exactly which fields to import, and in which order.
- Writes the result to a CSV file on disk.
- Optionally loads that CSV directly into QGIS as a layer — and on repeated runs, **reloads the existing layer instead of creating a duplicate**, so that joins you've built against it (e.g. to a geometry layer) are preserved.
- Handles Airtable's array-type fields (multiple select, linked records, lookups, attachments) automatically, converting them into readable, comma-separated text.
- Remembers your last-used settings (Base ID, Table ID, View ID, field list, output path) **per QGIS project**, so repeated syncs require minimal re-entry.

This tool does **not** create geometry. It always produces a plain attribute table. If your Airtable base contains coordinate fields, see [Working with coordinates](#working-with-coordinates) below.

---

## How your API key is handled

Your Airtable API key is **never stored in plain text** and never written into the `.qgz`/`.qgs` project file.

Instead, the script uses QGIS' built-in, encrypted **Authentication Database**:

- The key is entered once into a QGIS "Basic authentication" configuration, which QGIS encrypts and stores in your local profile, protected by a master password you set.
- Only a short, meaningless reference ID to that configuration is saved in the project file — this ID is useless to anyone without access to your local, encrypted authentication database and your master password.
- All other settings (Base ID, Table ID, View ID, field list) are stored in plain text in the project file, as they are not considered sensitive on their own.

See [Setup](#setup) below for how to create the authentication configuration.

---

## Requirements

- QGIS 3.x with the Processing Toolbox
- Python `requests` library (included with most QGIS installations; if missing, install via the OSGeo4W shell: `pip install requests`)
- An Airtable account with API access to the base you want to import from

---

## Installation

1. Download `airtable2qgis.py` from this repository.
2. In QGIS, open **Processing → Toolbox**.
3. At the bottom of the toolbox, click the **Scripts** icon → **Add Script to Toolbox...** (or open the Script Editor and paste the contents, then save it into your `processing/scripts` folder).
4. The tool will appear in the toolbox under **Scripts → Airtable2QGIS**.

---

## Setup

### 1. Create an Airtable API key

1. Go to [airtable.com/create/tokens](https://airtable.com/create/tokens).
2. Click **Create new token**.
3. Add the scope **`data.records:read`**.
4. Under **Access**, select the base you want to import from.
5. Create the token and copy it immediately — it is shown only once.

### 2. Store the key in QGIS' Authentication Database

1. Open the **Airtable2QGIS** tool from the Processing Toolbox.
2. In the **"Airtable authentication"** field, click the green **+** button.
3. Choose **Basic authentication** as the authentication method.
4. Choose a **Username** and paste your Airtable API key into the **Password** field.
5. Give the configuration a name, e.g. `Airtable API Key`, and save it.
6. If this is your first time using the Authentication Database, QGIS will ask you to set a **master password** for your QGIS profile. This protects everything stored this way — remember it, as it cannot be recovered.

Once created, this configuration is reusable across projects and other tools, and will be pre-selected automatically the next time you run this tool in the same project.

---

## Finding your Base ID, Table ID, and View ID

| Value | Where to find it |
|---|---|
| **Base ID** | Open your base in the browser. The URL looks like `airtable.com/appXXXXXXXXXXXXXX/...` — the part starting with `app` is the Base ID. |
| **Table ID** | Open the relevant table. The URL extends to `.../tblXXXXXXXXXXXXXX/...` — the part starting with `tbl` is the Table ID. |
| **View ID** *(optional)* | Open the relevant view. The URL extends further to `.../viwXXXXXXXXXXXXXX?...` — the part starting with `viw` is the View ID (ignore any `?...` suffix after it). If left empty, all records of the table are imported. |

---

## Usage

### Parameters

| Parameter | Required | Description |
|---|---|---|
| Airtable authentication | Yes | The saved authentication configuration containing your API key (see [Setup](#setup)). |
| Base ID | Yes | See table above. |
| Table ID | Yes | See table above. |
| View ID | No | See table above. Restricts the import to records visible in this view. |
| Fields | No | Comma-separated list of field names, exactly as spelled in Airtable, in the desired column order (e.g. `Name, Status, Date`). Leave empty to import all fields, in the order returned by the API. |
| Output CSV file | Yes | Where the resulting CSV file is written. |
| Load layer into QGIS after import | Yes | See workflow below. |

### Workflow

**First import:**
1. Fill in authentication, Base ID, Table ID, and (optionally) View ID / Fields.
2. Choose a file path for the CSV output.
3. Enable **"Load layer into QGIS after import."**
4. Run the tool. A new layer is added to your project.
5. Set up any joins (e.g. to a geometry layer) against this new layer as usual.

**Subsequent imports (updates):**
1. Run the tool again with the same output path.
2. **Leave "Load layer into QGIS after import" disabled.**
3. The CSV file is overwritten in place. The existing layer already points to this file, so any joins remain intact.
4. If the update isn't reflected immediately, right-click the layer → **Reload**, or press `F5`.

> Re-enabling "Load layer" on a re-run is safe (it detects the existing layer by file path and reloads it instead of duplicating it), but leaving it off avoids unnecessary processing if you already have the layer loaded.

---

## Working with coordinates

This tool always imports data as a plain attribute table, without geometry. If your Airtable base contains coordinate fields (e.g. `X`/`Y`, `Longitude`/`Latitude`), you can generate point geometry from the imported table using QGIS' native **"Create points layer from table"** algorithm:

1. Run Airtable2QGIS first to produce the attribute table / CSV layer.
2. Open **Processing Toolbox → Vector creation → Create points layer from table**.
3. Select the layer produced by Airtable2QGIS, specify your X and Y field names and the correct EPSG code.

---

## Known limitations

- Airtable's REST API is limited to **5 requests per second per base**, regardless of your Airtable plan. Very large tables may take a while to import due to pagination, and the tool will automatically wait and retry if this limit is hit.
- Attachment fields are reduced to their file URL(s); the files themselves are not downloaded.
- Multi-select, linked record, and lookup fields (which Airtable returns as arrays) are automatically joined into a single, comma-separated text value.
- Geometry import (points from coordinate fields, or WKT/GeoJSON geometry stored in Airtable) is not built in — see [Working with coordinates](#working-with-coordinates) for a native QGIS workaround for point data.

---

## License

MIT - License

## Author

Th. Leutgeb — Stadtarchäologie Wien
