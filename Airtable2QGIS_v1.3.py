"""
Airtable2QGIS_v1.3
Imports an Airtable table or view as a CSV file into QGIS (QGIS Processing script).

Author:  Th. Leutgeb, Stadtarchäologie Wien
License: MIT
"""

from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingParameterString,
    QgsProcessingParameterAuthConfig, QgsProcessingParameterFileDestination,
    QgsProcessingParameterBoolean, QgsProject, QgsVectorLayer,
    QgsVectorLayerJoinInfo, QgsProcessingException, QgsApplication,
    QgsAuthMethodConfig, QgsDataProvider
)
import requests
import csv
import os
import re
import time
import urllib.parse

# The version is defined here only. It determines the name shown in the
# toolbox, the log header and the key under which the inputs are stored in
# the project file (so after an update the inputs have to be entered once).
VERSION = "1.3"
TOOL_NAME = f"Airtable2QGIS_v{VERSION}"
PROJECT_SCOPE = TOOL_NAME

AIRTABLE_TIMEOUT = 15      # seconds per request
RATE_LIMIT_WAIT = 30       # seconds to wait after HTTP 429
RATE_LIMIT_RETRIES = 5     # give up after this many consecutive 429 responses


# ---------------------------------------------------------------------------
# Helpers without QGIS dependencies (easy to test on their own)
# ---------------------------------------------------------------------------

def source_to_path(source):
    """Turn a delimitedtext layer source into a normalised file path.

    Example: 'file:///D:/data/my%20file.csv?delimiter=;' -> 'D:\\data\\my file.csv'
    """
    path = source.split('?')[0].split('|')[0]
    if path.startswith("file://"):
        path = path[len("file://"):]
    path = urllib.parse.unquote(path)
    if re.match(r"^/[A-Za-z]:", path):        # Windows: '/D:/data' -> 'D:/data'
        path = path[1:]
    elif os.name != "nt" and path.startswith("//"):   # Unix: '////home/x' -> '/home/x'
        path = "/" + path.lstrip("/")
    return os.path.normcase(os.path.normpath(path))


def build_csv_uri(path):
    """Build a delimitedtext URI for the CSV written by this tool.

    The path is URL-encoded, otherwise special characters (e.g. 'ß', spaces)
    make the layer fail to load.
    """
    encoded = urllib.parse.quote(path.replace("\\", "/"), safe=':/')
    if not encoded.startswith("/"):            # Windows: 'D:/...'
        encoded = "/" + encoded
    return (f"file://{encoded}?delimiter=;&encoding=UTF-8"
            f"&useHeader=yes&detectTypes=yes")


def cell_to_text(value):
    """Convert an Airtable field value into a CSV cell."""
    if isinstance(value, list):
        # Attachment fields contain dicts (url, filename, ...): keep the URL.
        # Other array fields (multi-select, linked records, lookups) contain
        # plain values.
        if value and isinstance(value[0], dict):
            return ", ".join(v.get("url", "") for v in value)
        return ", ".join(str(v) for v in value)
    if value is None:
        return ""
    return str(value)


def collect_field_names(records):
    """All field names of the records, in order of first appearance."""
    return list(dict.fromkeys(key for r in records for key in r["fields"]))


def write_csv(path, records, field_names):
    # utf-8-sig + ';' so that Excel and QGIS both open the file correctly
    with open(path, mode='w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(field_names)
        for r in records:
            fields = r["fields"]
            writer.writerow([cell_to_text(fields.get(name, "")) for name in field_names])


def fetch_records(session, url, base_params, feedback):
    """Download all records page by page (Airtable returns max. 100 per page)."""
    records = []
    offset = None
    rate_limit_hits = 0

    while True:
        if feedback.isCanceled():
            raise QgsProcessingException("Cancelled by user.")

        params = list(base_params)
        if offset:
            params.append(("offset", offset))

        try:
            response = session.get(url, params=params, timeout=AIRTABLE_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise QgsProcessingException(f"Network error while contacting Airtable: {e}")

        if response.status_code == 429:
            rate_limit_hits += 1
            if rate_limit_hits > RATE_LIMIT_RETRIES:
                raise QgsProcessingException(
                    "Airtable rate limit was hit repeatedly. Please try again later.")
            feedback.pushInfo(
                f"Airtable rate limit hit (5 requests/second per base). "
                f"Waiting {RATE_LIMIT_WAIT} seconds before retrying...")
            for _ in range(RATE_LIMIT_WAIT):       # wait in 1 s steps, stay cancelable
                if feedback.isCanceled():
                    raise QgsProcessingException("Cancelled by user.")
                time.sleep(1)
            continue
        rate_limit_hits = 0

        try:
            data = response.json()
        except ValueError:
            raise QgsProcessingException(
                f"Unexpected (non-JSON) response from Airtable, HTTP status {response.status_code}.")

        if not response.ok or "error" in data:
            raise QgsProcessingException(
                f"Airtable API error (HTTP {response.status_code}): {data.get('error', data)}")

        records.extend(data.get("records", []))
        offset = data.get("offset")
        feedback.pushInfo(f"Fetched {len(records)} record(s) so far...")

        if not offset:
            return records


def load_api_key(auth_cfg_id):
    """Read the API key from the QGIS Authentication Database."""
    auth_cfg = QgsAuthMethodConfig()
    found = QgsApplication.authManager().loadAuthenticationConfig(
        auth_cfg_id, auth_cfg, full=True)
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
    return api_key


# ---------------------------------------------------------------------------
# Processing algorithm
# ---------------------------------------------------------------------------

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
        last_refresh_joins = project.readBoolEntry(PROJECT_SCOPE, "last_refresh_joins", True)[0]

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
        self.addParameter(QgsProcessingParameterBoolean(
            'REFRESH_JOINS',
            'Refresh joins of already loaded layers (rebuilds the join cache)',
            defaultValue=last_refresh_joins))

    # -- background thread --------------------------------------------------
    # processAlgorithm runs in a background thread. It must not touch the
    # QGIS project or its layers; that happens in postProcessAlgorithm.

    def processAlgorithm(self, parameters, context, feedback):
        auth_cfg_id = self.parameterAsString(parameters, 'AUTH_CFG', context).strip()
        base_id = self.parameterAsString(parameters, 'BASE_ID', context).strip()
        table_id = self.parameterAsString(parameters, 'TABLE_ID', context).strip()
        view_id = self.parameterAsString(parameters, 'VIEW_ID', context).strip()
        fields_input = self.parameterAsString(parameters, 'FIELDS', context).strip()
        dest_path = self.parameterAsFileOutput(parameters, 'OUTPUT', context)
        load_layer = self.parameterAsBoolean(parameters, 'LOAD_LAYER', context)
        refresh_joins = self.parameterAsBoolean(parameters, 'REFRESH_JOINS', context)

        # Handed over to postProcessAlgorithm (main thread). Only the auth
        # config ID is remembered, never the API key itself.
        self._dest_path = dest_path
        self._load_layer = load_layer
        self._refresh_joins = refresh_joins
        self._settings = {
            "last_auth_cfg": auth_cfg_id,
            "last_base_id": base_id,
            "last_table_id": table_id,
            "last_view_id": view_id,
            "last_fields": fields_input,
            "last_output_path": dest_path,
        }

        feedback.pushInfo(TOOL_NAME)

        if not auth_cfg_id or not base_id or not table_id:
            raise QgsProcessingException(
                "Authentication, Base ID and Table ID are required. "
                "See the help text (top right of this dialog) for where to find them."
            )

        api_key = load_api_key(auth_cfg_id)
        field_order = [f.strip() for f in fields_input.split(',') if f.strip()]

        base_params = [("fields[]", f) for f in field_order]
        if view_id:
            base_params.append(("view", view_id))

        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {api_key}"
        records = fetch_records(
            session, f"https://api.airtable.com/v0/{base_id}/{table_id}",
            base_params, feedback)
        feedback.pushInfo(f"{len(records)} record(s) retrieved from Airtable in total.")

        # Explicit field list = column order; otherwise all fields found
        write_csv(dest_path, records, field_order or collect_field_names(records))
        feedback.pushInfo(f"CSV file written: {dest_path}")

        return {'OUTPUT': dest_path}

    # -- main thread --------------------------------------------------------

    def postProcessAlgorithm(self, context, feedback):
        project = QgsProject.instance()

        self._remember_inputs(project)

        csv_layers = self._find_csv_layers(project)
        for layer in csv_layers:
            self._reread_layer(layer, feedback)

        if csv_layers:
            if self._refresh_joins:
                self._rebuild_joins(project, csv_layers, feedback)
            else:
                feedback.pushInfo("Join refresh skipped (checkbox disabled). "
                                  "Joined layers may still show cached values.")

        if self._load_layer:
            if csv_layers:
                feedback.pushInfo("Layer already in project, not added again.")
            else:
                self._add_csv_layer(project, feedback)
        else:
            feedback.pushInfo("Layer loading skipped (checkbox disabled). "
                              "Only the CSV file was updated.")

        return {'OUTPUT': self._dest_path}

    def _remember_inputs(self, project):
        """Store the inputs in the project so they are pre-filled next time."""
        for key, value in self._settings.items():
            project.writeEntry(PROJECT_SCOPE, key, value)
        project.writeEntryBool(PROJECT_SCOPE, "last_load_layer", self._load_layer)
        project.writeEntryBool(PROJECT_SCOPE, "last_refresh_joins", self._refresh_joins)

    def _find_csv_layers(self, project):
        """All CSV layers in the project that point to the output file."""
        target = source_to_path(self._dest_path)
        return [
            layer for layer in project.mapLayers().values()
            if isinstance(layer, QgsVectorLayer)
            and layer.providerType() == "delimitedtext"
            and source_to_path(layer.source()) == target
        ]

    def _reread_layer(self, layer, feedback):
        """Make sure the layer has read the new file content before the joins
        are rebuilt. reload() alone may not re-read the file immediately, so
        the data provider is recreated."""
        try:
            layer.setDataSource(
                layer.source(), layer.name(), layer.providerType(),
                QgsDataProvider.ProviderOptions()
            )
            feedback.pushInfo(
                f"Existing layer '{layer.name()}' re-read from file "
                f"({layer.featureCount()} feature(s))."
            )
        except Exception as e:
            layer.reload()
            feedback.pushInfo(
                f"Layer '{layer.name()}' reloaded (forced re-read failed: {e}).")

    def _rebuild_joins(self, project, csv_layers, feedback):
        """Rebuild all joins that use one of the CSV layers as join source.

        Why this is necessary: with 'cache join layer in virtual memory'
        QGIS copies the joined values once and only rebuilds that copy when
        the cache is marked as 'dirty'. This happens only when the join layer
        emits layerModified, which neither reload() nor setDataSource() does,
        so joined layers keep showing the old values until the join is
        recreated manually.
        """
        start = time.perf_counter()
        csv_layer_ids = {layer.id() for layer in csv_layers}

        # Mark the caches as dirty ...
        for layer in csv_layers:
            layer.layerModified.emit()

        # ... and recreate the joins from fresh join objects (a copy of the
        # old join object could carry its old, 'clean' cache along).
        rebuilt = 0
        for target in project.mapLayers().values():
            if not isinstance(target, QgsVectorLayer):
                continue
            for old in list(target.vectorJoins()):
                if old.joinLayerId() not in csv_layer_ids:
                    continue
                target.removeJoin(old.joinLayerId())
                target.addJoin(self._copy_join_settings(old))
                target.triggerRepaint()
                rebuilt += 1
                feedback.pushInfo(f"Join on layer '{target.name()}' rebuilt.")

        feedback.pushInfo(
            f"Join refresh: {rebuilt} join(s) in {time.perf_counter() - start:.2f} s.")

    @staticmethod
    def _copy_join_settings(old):
        """New join object with the same settings as 'old' but without its cache."""
        info = QgsVectorLayerJoinInfo()
        info.setJoinLayerId(old.joinLayerId())
        info.setJoinFieldName(old.joinFieldName())
        info.setTargetFieldName(old.targetFieldName())
        info.setPrefix(old.prefix())
        info.setUsingMemoryCache(old.isUsingMemoryCache())
        info.setEditable(old.isEditable())
        info.setUpsertOnEdit(old.hasUpsertOnEdit())
        info.setCascadedDelete(old.hasCascadedDelete())
        info.setDynamicFormEnabled(old.isDynamicFormEnabled())
        info.setJoinFieldNamesSubset(old.joinFieldNamesSubset())
        if old.joinLayer() is not None:
            info.setJoinLayer(old.joinLayer())
        return info

    def _add_csv_layer(self, project, feedback):
        layer_name = os.path.splitext(os.path.basename(self._dest_path))[0]
        new_layer = QgsVectorLayer(build_csv_uri(self._dest_path), layer_name, "delimitedtext")
        if new_layer.isValid():
            project.addMapLayer(new_layer)
            feedback.pushInfo(f"New layer '{layer_name}' added to project.")
        else:
            feedback.reportError("Layer could not be loaded (invalid source).")

    # -- metadata -----------------------------------------------------------

    def name(self):
        # Internal ID (lowercase, no spaces). Used by the processing history,
        # models and processing.run(); it is not shown in the toolbox.
        return 'airtable2qgis'

    def displayName(self):
        # Name shown in the toolbox and in the dialog title
        return TOOL_NAME

    def shortHelpString(self):
        return (
            "<p>Imports records from an Airtable view as a CSV table and "
            "optionally loads it as a layer into QGIS. All input values "
            "(authentication, Base ID, Table ID, View ID, field list, and "
            "the checkboxes) are stored per project and automatically "
            "pre-filled the next time this tool is opened in the same "
            "project.</p>"

            "<p><b>Setting up authentication (one-time step):</b></p>"
            "<p>"
            "This tool uses QGIS' built-in, encrypted Authentication "
            "Database to store your Airtable API key — it is never written "
            "in plain text.<br><br>"
            "1. In the 'Airtable authentication' field, click the green "
            "'+' button.<br>"
            "2. Choose <b>'Basic authentication'</b> as the authentication "
            "type.<br>"
            "3. Enter <b>any</b> 'Username' (e.g. <i>airtable</i>) and paste "
            "your Airtable API key into the <b>'Password'</b> field. The "
            "username is not used, but QGIS keeps the 'Save' button "
            "disabled as long as it is empty.<br>"
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
            "(i.e. updates), leave that checkbox disabled — the file at the "
            "chosen path is overwritten, and the existing layer in QGIS "
            "already points to it. The existing layer is reloaded "
            "automatically.</p>"

            "<p><b>Joins:</b> If the CSV layer is used as a join source, "
            "QGIS keeps a cached copy of its values, which is not updated "
            "by a layer reload or F5. With 'Refresh joins' enabled "
            "(default), the tool rebuilds these joins after each import so "
            "that the joined layers show the new values. The time needed is "
            "written to the log. Disable the option if you do not use "
            "joins, or if the refresh takes too long for your data. The "
            "option only has an effect if the CSV layer is already loaded "
            "in the project; it does nothing when the layer is added in the "
            "same run.</p>"

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
