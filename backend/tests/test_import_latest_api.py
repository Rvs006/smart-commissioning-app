"""GET /imports/latest — the Setup card's "register already imported" lookup.

The frontend (getLatestImport) asks for the newest usable import of a type for a
project/site so the empty native file input does not imply nothing was ever
uploaded (ISSUE-5). These pin the HTTP contract: the literal /latest path is not
swallowed by GET /{import_id}, a hit returns the stored summary, a
fully-rejected import does not count as "on file", and an absent one is a 404.
"""

import io

from harness import ApiTestCase

_HEADER = (
    "Project/site,System,Asset ID,Expected topic,Expected schema version,"
    "Expected points,Expected units,Expected reporting interval,Source protocol"
)
_ROW = "Site A,BMS,FCU-04,site/b1/fcu-04/#,1.5.2,supply_air_temp,degrees-celsius,60,MQTT"

_API_KEY = "test-import-latest-key"
# Distinct project/site so the shared per-process database never leaks this
# register into other test classes' lookups (or theirs into ours).
_PROJECT = "import-latest-project"
_SITE = "import-latest-site"


class ImportLatestApiTests(ApiTestCase):
    env = {"AUTH_MODE": "api_key", "API_KEY": _API_KEY}
    client_headers = {"X-API-Key": _API_KEY}

    def _upload(self, data: bytes, *, project_id: str, site_id: str) -> object:
        return self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", "project_id": project_id, "site_id": site_id},
            files={"file": ("register.csv", io.BytesIO(data), "text/csv")},
        )

    def _latest(self, import_type: str, *, project_id: str, site_id: str) -> object:
        return self.client.get(
            "/api/v1/imports/latest",
            params={"import_type": import_type, "project_id": project_id, "site_id": site_id},
        )

    def test_latest_returns_the_stored_summary_after_an_upload(self) -> None:
        upload = self._upload((_HEADER + "\n" + _ROW + "\n").encode(), project_id=_PROJECT, site_id=_SITE)
        self.assertEqual(upload.status_code, 200, upload.text)
        import_id = upload.json()["import_id"]

        response = self._latest("mqtt_register", project_id=_PROJECT, site_id=_SITE)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # The literal /latest path resolved to the lookup, NOT to GET /{import_id}
        # with import_id="latest" (which would 404). It returns the real record.
        self.assertEqual(body["import_id"], import_id)
        self.assertEqual(body["import_type"], "mqtt_register")
        self.assertEqual(body["accepted_rows"], 1)

    def test_latest_is_404_when_no_import_is_on_file(self) -> None:
        response = self._latest("ip_register", project_id=_PROJECT, site_id=_SITE)

        self.assertEqual(response.status_code, 404, response.text)

    def test_fully_rejected_import_does_not_count_as_on_file(self) -> None:
        # A header-only upload accepts zero rows; nothing a run could use is on
        # file, so the "already imported" lookup must still be a 404.
        project = "import-latest-rejected-project"
        upload = self._upload(_HEADER.encode(), project_id=project, site_id=_SITE)
        self.assertEqual(upload.status_code, 200, upload.text)
        self.assertEqual(upload.json()["accepted_rows"], 0, upload.text)

        response = self._latest("mqtt_register", project_id=project, site_id=_SITE)

        self.assertEqual(response.status_code, 404, response.text)
