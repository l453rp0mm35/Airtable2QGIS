from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingParameterString,
    QgsProcessingParameterAuthConfig, QgsProcessingParameterFileDestination,
    QgsProcessingParameterBoolean, QgsProject, QgsVectorLayer,
    QgsProcessingException, QgsApplication, QgsAuthMethodConfig
)
import requests
import csv
import os
import time
import urllib.parse

# Scope name for project-based settings
PROJECT_SCOPE = "Airtable2QGIS_v1.2"


class Airtable2QGISAlgorithm(QgsProcessingAlgorithm):

    def initAlgorithm(self, config=None):
        project = QgsProject.instance()

        last_auth_cfg, _ = project.readEntry(PROJECT_SCOPE, "last_auth_cfg", "")
        last_base_id, _ = project.readEntry(PROJECT_SCOPE, "last_base_id", "")
        last_table_id, _ = project.readEntry(PROJECT_SCOPE, "last_table_id", "")
        last_view_id, _ = project.readEntry(PROJECT_SCOPE, "last_view_id", "")
        last_fields, _ = project.readEntry(PROJECT_SCOPE, "last_fields", "")
        last_output_path, _ = project.readEntry(PROJECT_SCOPE, "last_output_path", "")
        last_load_layer = project.readBoolEntry(PROJECT_SCOPE, "last_load_layer", True)[0]

        self.addParameter(QgsProcessingParameterAuthConfig(
            'AUTH_CFG', 'Airtable authentication',
            defaultValue=last_auth_cfg if last_auth_cfg else None))
        self.addParameter(QgsProcessingParameterString(
            'BASE_ID', 'Base ID', defaultValue=last_base_id))
        self.addParameter(QgsProcessingParameterString(
            'TABLE_ID', 'Table ID', defaultValue=last_table_id))
        self.addParameter(QgsProcessingParameterString(
            'VIEW_ID', 'View ID', defaultValue=last_view_id, optional=True))
        self.addParameter(QgsProcessingParameterString(
            'FIELDS',
            'Fields to import (comma-separated, in desired order; leave empty for all fields)',
            defaultValue=last_fields, optional=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            'OUTPUT', 'Output CSV file', fileFilter='CSV files (*.csv)',
            defaultValue=last_output_path if last_output_path else None))
        self.addParameter(QgsProcessingParameterBoolean(
            'LOAD_LAYER', 'Load layer into QGIS after import',
            defaultValue=last_load_layer))

    def processAlgorithm(self, parameters, context, feedback):
        project = QgsProject.instance()

        auth_cfg_id = self.parameterAsString(parameters, 'AUTH_CFG', context).strip()
        base_id = self.parameterAsString(parameters, 'BASE_ID', context).strip()
        table_id = self.parameterAsString(parameters, 'TABLE_ID', context).strip()
        view_id = self.parameterAsString(parameters, 'VIEW_ID', context).strip()
        fields_input = self.parameterAsString(parameters, 'FIELDS', context).strip()
        dest_path = self.parameterAsFileOutput(parameters, 'OUTPUT', context)
        load_layer = self.parameterAsBoolean(parameters, 'LOAD_LAYER', context)

        # Save current inputs to the project for next time.
        # Only the auth config ID is stored (not the API key itself) -
        # the ID is meaningless without access to the encrypted auth
        # database and the QGIS profile's master password.
        project.writeEntry(PROJECT_SCOPE, "last_auth_cfg", auth_cfg_id)
        project.writeEntry(PROJECT_SCOPE, "last_base_id", base_id)
        project.writeEntry(PROJECT_SCOPE, "last_table_id", table_id)
        project.writeEntry(PROJECT_SCOPE, "last_view_id", view_id)
        project.writeEntry(PROJECT_SCOPE, "last_fields", fields_input)
        project.writeEntryBool(PROJECT_SCOPE, "last_load_layer", load_layer)

        # Validate required parameters before calling the API
        if not auth_cfg_id or not base_id or not table_id:
            raise QgsProcessingException(
                "Authentication, Base ID and Table ID are required. "
                "See the help text (top right of this dialog) for where to find them."
            )

        # Resolve the API key from the QGIS Authentication Database
        auth_manager = QgsApplication.authManager()
        auth_cfg = QgsAuthMethodConfig()
        found = auth_manager.loadAuthenticationConfig(auth_cfg_id, auth_cfg, full=True)
        if not found or not auth_cfg.isValid():
            raise QgsProcessingException(
                "The selected authentication configuration could not be loaded. "
                "It may have been deleted, or the QGIS master password was not entered."
            )
        api_key = auth_cfg.config('password')
        if not api_key:
            raise QgsProcessingException(
                "Could not retrieve an API key from the selected authentication "
                "configuration. Make sure the key was entered in the 'Password' "
                "field when the configuration was created."
            )

        field_order = [f.strip() for f in fields_input.split(',') if f.strip()] if fields_input else []

        url = f"https://api.airtable.com/v0/{base_id}/{table_id}"
        headers = {"Authorization": f"Bearer {api_key}"}

        base_params = [("fields[]", f) for f in field_order]
        if view_id:
            base_params.append(("view", view_id))

        all_records = []
        offset = None
        while True:
            # Allow the user to cancel a long-running import
            if feedback.isCanceled():
                raise QgsProcessingException("Cancelled by user.")

            params = list(base_params)
            if offset:
                params.append(("offset", offset))

            # Request timeout to avoid hanging indefinitely
            try:
                raw_resp = requests.get(url, headers=headers, params=params, timeout=15)
            except requests.exceptions.RequestException as e:
                raise QgsProcessingException(f"Network error while contacting Airtable: {e}")

            # Handle rate limiting (HTTP 429) with a retry instead of failing
            if raw_resp.status_code == 429:
                feedback.pushInfo("Airtable rate limit hit (5 requests/second per base). Waiting 30 seconds before retrying...")
                time.sleep(30)
                continue

            resp = raw_resp.json()

            # Abort on API error instead of silently writing partial data
            if "error" in resp:
                raise QgsProcessingException(f"Airtable API error: {resp['error']}")

            all_records.extend(resp.get("records", []))
            offset = resp.get("offset")

            # Progress feedback for large tables
            feedback.pushInfo(f"Fetched {len(all_records)} record(s) so far...")

            if not offset:
                break

        feedback.pushInfo(f"{len(all_records)} record(s) retrieved from Airtable in total.")

        # If no explicit field list was given, derive column order from all records
        if field_order:
            all_keys = field_order
        else:
            all_keys = []
            for r in all_records:
                for key in r["fields"].keys():
                    if key not in all_keys:
                        all_keys.append(key)

        with open(dest_path, mode='w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(all_keys)
            for r in all_records:
                row = []
                for key in all_keys:
                    value = r["fields"].get(key, "")
                    if isinstance(value, list):
                        # Attachment fields contain dicts (url, filename, ...);
                        # extract just the URL. Other array fields (multi-select,
                        # linked records, lookups) contain plain strings.
                        if value and isinstance(value[0], dict):
                            value = ", ".join(v.get("url", "") for v in value)
                        else:
                            value = ", ".join(str(v) for v in value)
                    elif value is None:
                        value = ""
                    row.append(str(value))
                writer.writerow(row)

        feedback.pushInfo(f"CSV file written: {dest_path}")

        if load_layer:
            def normalize(p):
                return os.path.normcase(os.path.normpath(urllib.parse.unquote(p)))

            existing_layer = None
            for layer in project.mapLayers().values():
                layer_path = layer.source().split('|')[0].replace("file:///", "")
                if normalize(layer_path) == normalize(dest_path):
                    existing_layer = layer
                    break

            if existing_layer:
                existing_layer.reload()
                feedback.pushInfo(f"Existing layer '{existing_layer.name()}' reloaded (joins are preserved).")
            else:
                encoded_path = urllib.parse.quote(dest_path, safe=':/')
                uri = (
                    f"file:///{encoded_path}?delimiter=;&encoding=UTF-8"
                    f"&useHeader=yes&detectTypes=yes"
                )
                layer_name = os.path.splitext(os.path.basename(dest_path))[0]
                new_layer = QgsVectorLayer(uri, layer_name, "delimitedtext")
                if new_layer.isValid():
                    project.addMapLayer(new_layer)
                    feedback.pushInfo(f"New layer '{layer_name}' added to project.")
                else:
                    feedback.reportError("Layer could not be loaded (invalid source).")
        else:
            feedback.pushInfo("Layer loading skipped (checkbox disabled). Only the CSV file was updated.")

        project.writeEntry(PROJECT_SCOPE, "last_output_path", dest_path)

        return {'OUTPUT': dest_path}

    def name(self):
        return 'airtable2qgis'

    def displayName(self):
        return 'Airtable2QGIS_v1.2'

    def shortHelpString(self):
        return (
            "<p>Imports records from an Airtable view as a CSV table and "
            "optionally loads it as a layer into QGIS. All input values "
            "(authentication, Base ID, Table ID, View ID, field list, and "
            "the 'Load layer' checkbox) are stored per project and "
            "automatically pre-filled the next time this tool is opened in "
            "the same project.</p>"

            "<p><b>Setting up authentication (one-time step):</b></p>"
            "<p>"
            "This tool uses QGIS' built-in, encrypted Authentication "
            "Database to store your Airtable API key — it is never written "
            "in plain text.<br><br>"
            "1. In the 'Airtable authentication' field, click the green "
            "'+' button.<br>"
            "2. Choose <b>'Basic authentication'</b> as the authentication "
            "type.<br>"
            "3. Leave 'Username' empty (or enter anything, it is not used), "
            "and paste your Airtable API key into the <b>'Password'</b> "
            "field.<br>"
            "4. Give the configuration a recognisable name, e.g. "
            "<i>'Airtable API Key'</i>, and save it.<br>"
            "5. If this is the first time you use the Authentication "
            "Database, QGIS will ask you to set a master password for your "
            "QGIS profile. This password protects all credentials stored "
            "this way, not just this one — remember it, as it cannot be "
            "recovered.<br><br>"
            "Once created, the configuration is reusable across projects "
            "and other tools, and will be pre-selected automatically the "
            "next time you run this tool in the same project."
            "</p>"

            "<p><b>Where to find each input value in Airtable:</b></p>"
            "<p>"
            "<b>API Key</b> (Personal Access Token): Go to "
            "airtable.com/create/tokens, create a new token with the scope "
            "<i>data.records:read</i>, and grant it access to the relevant "
            "base. Copy the token immediately, as it is shown only once, "
            "then paste it into the authentication configuration described "
            "above.<br><br>"
            "<b>Base ID</b>: Open your base in the browser. The URL has the "
            "form airtable.com/appXXXXXXXXXXXXXX/... — the part starting "
            "with 'app' is the Base ID.<br><br>"
            "<b>Table ID</b>: Open the relevant table. The URL extends to "
            "airtable.com/appXXXXXXXXXXXXXX/tblXXXXXXXXXXXXXX/... — the part "
            "starting with 'tbl' is the Table ID.<br><br>"
            "<b>View ID</b> (optional): Open the relevant view. The URL "
            "extends further to .../viwXXXXXXXXXXXXXX?... — the part "
            "starting with 'viw' is the View ID (ignore any '?...' suffix "
            "after it). If left empty, all records of the table are "
            "imported.<br><br>"
            "<b>Fields</b> (optional): Comma-separated list of field names, "
            "exactly as spelled in Airtable, in the desired column order "
            "(e.g. <i>Name, Status, Date</i>). Leave empty to import all "
            "fields, in the order returned by the API."
            "</p>"

            "<p><b>Workflow:</b> On first import, specify a file path for "
            "the CSV output and enable the 'Load layer into QGIS after "
            "import' checkbox. For subsequent imports of the same dataset "
            "(i.e. updates), leave the checkbox disabled — the file at the "
            "chosen path is overwritten, and the existing layer in QGIS "
            "already points to it. If updates are not visible immediately, "
            "press 'F5' or use the 'Reload' button on the layer.</p>"

            "<p><b>Note:</b> Multi-select, linked record, and lookup fields "
            "in Airtable are returned as arrays and are automatically joined "
            "into a single, comma-separated text value. Attachment fields "
            "are reduced to their file URL(s).</p>"

            "<p><b>Working with coordinates:</b> This tool always imports "
            "data as a plain attribute table, without geometry. If your "
            "Airtable base contains coordinate fields (e.g. X/Y, "
            "Longitude/Latitude), you can generate point geometry from the "
            "imported table using QGIS' native "
            "<b>'Create points layer from table'</b> algorithm (Processing "
            "Toolbox → Vector creation → Create points layer from table). "
            "Run Airtable2QGIS first to produce the attribute table, then "
            "run that native algorithm on the resulting layer, specifying "
            "your X and Y field names and the correct EPSG code.</p>"

            "<p><b>Security notice:</b> Your Airtable API key is stored "
            "securely and encrypted in QGIS' Authentication Database, "
            "protected by your profile's master password — it is not "
            "written into this project file. Only a harmless reference ID "
            "to the stored configuration is saved in the project, which is "
            "useless without access to your local, encrypted authentication "
            "database. All other input values (Base ID, Table ID, View ID, "
            "field list) are stored in plain text in this project file, as "
            "they are not considered sensitive on their own.</p>"
        )

    def createInstance(self):
        return Airtable2QGISAlgorithm()