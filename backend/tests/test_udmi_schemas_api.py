"""Non-published UDMI schema set API (/api/v1/udmi/schemas) + run embedding.

Covers the upload/list/delete lifecycle, upload-time validation (nonpub-shaped
label incl. its length cap, required root files, JSON + Draft 7 well-formedness,
supported $ref forms, dangling ``file:`` $refs, per-set file count, the
authoritative received-bytes total, and the stored-total ceiling), RBAC (viewer
reads, engineer writes), and the run-creation contract: a created UDMI
validation run's STORED parameters carry every uploaded set under
``nonpub_schema_sets`` (the DB store is the sole source — a client-supplied
copy is discarded) so the queued worker validates from the shared database
alone, while API responses serialize a filenames-only summary of the sets.

Each test uses its own version label and deletes what it uploads (addCleanup):
the per-process database is shared across test modules, and a lingering set
would be embedded into every later UDMI run's parameters.
"""

import io
import json
import unittest
import zipfile

from harness import ApiTestCase

_API_KEY = "test-udmi-schemas-admin-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}

_STATE_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["timestamp", "version", "system"],
    "properties": {
        "timestamp": {"type": "string"},
        "version": {"type": "string"},
        "system": {"type": "object"},
    },
}
_METADATA_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["timestamp", "version", "system"],
    "properties": {
        "timestamp": {"type": "string"},
        "version": {"type": "string"},
        "system": {"type": "object"},
    },
}
# The pointset root refs a sibling file, exercising the internal-$ref closure.
_POINTSET_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["timestamp", "version", "points"],
    "properties": {
        "timestamp": {"type": "string"},
        "version": {"type": "string"},
        "points": {
            "type": "object",
            "additionalProperties": {"$ref": "file:events_pointset_point.json#"},
        },
    },
}
_POINT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["present_value"],
}
# Pointset root with NO $refs, for uploads that omit the point schema file.
_POINTSET_SCHEMA_NO_REF = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["timestamp", "version", "points"],
    "properties": {"points": {"type": "object"}},
}


def _full_set() -> dict[str, bytes]:
    return {
        "state.json": json.dumps(_STATE_SCHEMA).encode(),
        "metadata.json": json.dumps(_METADATA_SCHEMA).encode(),
        "events_pointset.json": json.dumps(_POINTSET_SCHEMA).encode(),
        "events_pointset_point.json": json.dumps(_POINT_SCHEMA).encode(),
    }


def _roots_only_set() -> dict[str, bytes]:
    return {
        "state.json": json.dumps(_STATE_SCHEMA).encode(),
        "metadata.json": json.dumps(_METADATA_SCHEMA).encode(),
        "events_pointset.json": json.dumps(_POINTSET_SCHEMA_NO_REF).encode(),
    }


class UdmiSchemaSetApiTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    # -- helpers --------------------------------------------------------------

    def _post_set(
        self,
        label: str,
        files: dict[str, bytes],
        headers: dict[str, str] | None = None,
    ) -> object:
        response = self.client.post(
            "/api/v1/udmi/schemas",
            data={"version_label": label},
            files=[
                ("files", (name, io.BytesIO(content), "application/json"))
                for name, content in files.items()
            ],
            headers=headers,
        )
        if response.status_code == 200:
            # Stored sets outlive this test module in the shared per-process
            # database and would be embedded into every later UDMI run.
            self.addCleanup(
                self.client.delete,
                f"/api/v1/udmi/schemas/{response.json()['version_label']}",
            )
        return response

    def _listed_labels(self) -> dict[str, dict]:
        response = self.client.get("/api/v1/udmi/schemas")
        self.assertEqual(response.status_code, 200, response.text)
        return {entry["version_label"]: entry for entry in response.json()}

    def _create_viewer_key(self) -> str:
        response = self.client.post(
            "/api/v1/users",
            json={"username": "udmi-schemas-viewer", "role": "viewer"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["api_key"]

    # -- lifecycle ------------------------------------------------------------

    def test_upload_list_delete_roundtrip(self) -> None:
        response = self._post_set("nonpub.roundtrip", _full_set())
        self.assertEqual(response.status_code, 200, response.text)
        summary = response.json()
        self.assertEqual(summary["version_label"], "nonpub.roundtrip")
        self.assertEqual(
            summary["filenames"],
            sorted(["state.json", "metadata.json", "events_pointset.json", "events_pointset_point.json"]),
        )
        self.assertIn("uploaded_at", summary)
        self.assertEqual(summary["uploaded_by"], "shared-key")

        listed = self._listed_labels()
        self.assertIn("nonpub.roundtrip", listed)
        # Summaries only — never the stored schema content.
        self.assertEqual(
            set(listed["nonpub.roundtrip"]),
            {"version_label", "filenames", "uploaded_at", "uploaded_by"},
        )

        deleted = self.client.delete("/api/v1/udmi/schemas/nonpub.roundtrip")
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertNotIn("nonpub.roundtrip", self._listed_labels())
        # Deleting again is an honest 404.
        again = self.client.delete("/api/v1/udmi/schemas/nonpub.roundtrip")
        self.assertEqual(again.status_code, 404, again.text)

    def test_reupload_same_label_replaces_content(self) -> None:
        from app.core.db import get_engine
        from smart_commissioning_core.db.repositories import UdmiSchemaSetRepository

        # Casing drift between the upload form and the register column must not
        # create two rows: the stored label is the normalised nonpub key.
        first = self._post_set("NonPub.Replace", _full_set())
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["version_label"], "nonpub.replace")

        second = self._post_set("nonpub.replace", _roots_only_set())
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(
            second.json()["filenames"],
            sorted(["state.json", "metadata.json", "events_pointset.json"]),
        )

        listed = self._listed_labels()
        self.assertEqual(
            listed["nonpub.replace"]["filenames"],
            sorted(["state.json", "metadata.json", "events_pointset.json"]),
        )
        # The row's content was replaced wholesale, not merged.
        stored = UdmiSchemaSetRepository(get_engine()).get_all_files()["nonpub.replace"]
        self.assertNotIn("events_pointset_point.json", stored)
        self.assertEqual(stored["events_pointset.json"], _POINTSET_SCHEMA_NO_REF)

    # -- upload validation ----------------------------------------------------

    def test_non_nonpub_label_is_400(self) -> None:
        response = self._post_set("1.5.2", _full_set())
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("non-published", response.json()["detail"])

    def test_path_syntax_labels_are_400(self) -> None:
        for label in ("nonpub/../x", "nonpub\\x", "nonpub..1"):
            response = self._post_set(label, _full_set())
            self.assertEqual(response.status_code, 400, label)

    def test_missing_root_file_is_400_naming_it(self) -> None:
        files = _roots_only_set()
        files.pop("events_pointset.json")
        response = self._post_set("nonpub.missing-root", files)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("events_pointset.json", response.json()["detail"])

    def test_invalid_json_file_is_400(self) -> None:
        files = _roots_only_set()
        files["state.json"] = b"{not json"
        response = self._post_set("nonpub.bad-json", files)
        self.assertEqual(response.status_code, 400, response.text)
        detail = response.json()["detail"]
        self.assertIn("state.json", detail)
        self.assertIn("not valid JSON", detail)

    def test_non_object_json_file_is_400(self) -> None:
        files = _roots_only_set()
        files["metadata.json"] = b'["not", "a", "schema"]'
        response = self._post_set("nonpub.non-object", files)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("JSON object", response.json()["detail"])

    def test_invalid_draft7_schema_is_400(self) -> None:
        files = _roots_only_set()
        files["state.json"] = json.dumps({"type": "not-a-real-type"}).encode()
        response = self._post_set("nonpub.bad-schema", files)
        self.assertEqual(response.status_code, 400, response.text)
        detail = response.json()["detail"]
        self.assertIn("state.json", detail)
        self.assertIn("Draft 7", detail)

    def test_dangling_file_ref_is_400_naming_it(self) -> None:
        files = _roots_only_set()
        files["events_pointset.json"] = json.dumps(_POINTSET_SCHEMA).encode()
        # _POINTSET_SCHEMA refs events_pointset_point.json, which is not uploaded.
        response = self._post_set("nonpub.dangling-ref", files)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("events_pointset_point.json", response.json()["detail"])

    def test_unsupported_ref_forms_are_400_naming_the_ref(self) -> None:
        # The run-time resolver reaches only 'file:<name>.json' registry
        # entries (plus same-document fragments); every other form passes
        # Draft 7 well-formedness but explodes at validation time, so the
        # upload must reject it by name.
        for ref in (
            "events_pointset_point.json#",  # plain-relative
            "http://example.com/point.json",  # http
            "urn:example:point",  # urn
            "file:sub/point.json",  # path inside the name
            "file:point.txt#/definitions/point",  # not a .json name
        ):
            files = _roots_only_set()
            files["events_pointset.json"] = json.dumps(
                {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {"points": {"additionalProperties": {"$ref": ref}}},
                }
            ).encode()
            response = self._post_set("nonpub.bad-ref", files)
            self.assertEqual(response.status_code, 400, f"{ref}: {response.text}")
            detail = response.json()["detail"]
            self.assertIn(ref, detail)
            self.assertIn("file:<name>.json", detail)

    def test_same_document_ref_is_accepted(self) -> None:
        files = _roots_only_set()
        files["events_pointset.json"] = json.dumps(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "required": ["timestamp", "version", "points"],
                "definitions": {"point": {"type": "object"}},
                "properties": {
                    "points": {
                        "type": "object",
                        "additionalProperties": {"$ref": "#/definitions/point"},
                    }
                },
            }
        ).encode()
        response = self._post_set("nonpub.fragment-ref", files)
        self.assertEqual(response.status_code, 200, response.text)

    def test_overlong_version_label_is_400(self) -> None:
        label = "nonpub." + "x" * 249  # 256 chars after normalisation
        response = self._post_set(label, _roots_only_set())
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("255", response.json()["detail"])

    def test_more_than_64_schema_files_is_413(self) -> None:
        files = _roots_only_set()
        for index in range(62):
            files[f"extra_{index:02d}.json"] = b'{"type": "object"}'
        self.assertEqual(len(files), 65)
        response = self._post_set("nonpub.too-many-files", files)
        self.assertEqual(response.status_code, 413, response.text)
        self.assertIn("64", response.json()["detail"])

    def test_stored_sets_total_size_ceiling_is_413(self) -> None:
        # Each UDMI run embeds every stored set into its parameters row, so
        # the STORED total (not just this request) is capped at 2 MB.
        files = _roots_only_set()
        big_state = dict(_STATE_SCHEMA)
        big_state["description"] = "x" * 2_200_000
        files["state.json"] = json.dumps(big_state).encode()
        response = self._post_set("nonpub.too-big-overall", files)
        self.assertEqual(response.status_code, 413, response.text[:200])
        self.assertIn("ceiling", response.json()["detail"])
        # Nothing was stored.
        self.assertNotIn("nonpub.too-big-overall", self._listed_labels())

    # -- RBAC -----------------------------------------------------------------

    def test_viewer_can_read_but_not_write(self) -> None:
        viewer_headers = {"X-API-Key": self._create_viewer_key()}
        listed = self.client.get("/api/v1/udmi/schemas", headers=viewer_headers)
        self.assertEqual(listed.status_code, 200, listed.text)

        upload = self._post_set("nonpub.rbac", _full_set(), headers=viewer_headers)
        self.assertEqual(upload.status_code, 403, upload.text)
        delete = self.client.delete(
            "/api/v1/udmi/schemas/nonpub.rbac", headers=viewer_headers
        )
        self.assertEqual(delete.status_code, 403, delete.text)

    # -- run-creation embedding -----------------------------------------------

    def test_udmi_run_parameters_embed_uploaded_sets(self) -> None:
        upload = self._post_set("nonpub.run-embed", _full_set())
        self.assertEqual(upload.status_code, 200, upload.text)

        response = self.client.post(
            "/api/v1/validation/udmi/runs",
            json={
                "project_id": "udmi-schemas-embed-project",
                "site_id": "udmi-schemas-embed-site",
                "job_type": "udmi_validation",
                "parameters": {
                    "expected_schedule": {
                        "asset_id": "EM-1",
                        "udmi_version": "nonpub.run-embed",
                    },
                    # Declares the nonpub version but omits the required
                    # 'system', so the uploaded set must produce a finding.
                    "state_payload": {
                        "timestamp": "2026-07-14T10:00:00Z",
                        "version": "nonpub.run-embed",
                    },
                    "use_live_broker": False,
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        run = self.client.get(f"/api/v1/validation/runs/{run_id}").json()

        self.assertEqual(run["status"], "succeeded")
        # The API response serializes a filenames-only summary of the embedded
        # sets: every 1.5s status poll must not re-serve the full schema bodies.
        self.assertEqual(
            run["parameters"]["nonpub_schema_sets"],
            {
                "nonpub.run-embed": sorted(
                    ["state.json", "metadata.json", "events_pointset.json", "events_pointset_point.json"]
                )
            },
        )
        self.assertNotIn("$schema", json.dumps(run["parameters"]))
        # The STORED parameters (attribute access, what the Dramatiq worker
        # reads from the shared database) still carry the full uploaded set.
        from app.services.run_service import RunService

        raw_parameters = RunService().get_run(run_id).parameters
        self.assertEqual(
            raw_parameters["nonpub_schema_sets"]["nonpub.run-embed"]["state.json"],
            _STATE_SCHEMA,
        )
        # And the validation actually ran against the uploaded set.
        descriptions = " ".join(issue["description"] for issue in run["issues"])
        self.assertIn("'system'", descriptions)

    def test_client_supplied_schema_sets_are_ignored(self) -> None:
        # No uploaded set exists, and the client smuggles its own copy (which
        # never went through the upload route's validation): the run must not
        # honor it — the missing-set finding fires instead.
        response = self.client.post(
            "/api/v1/validation/udmi/runs",
            json={
                "project_id": "udmi-schemas-spoof-project",
                "site_id": "udmi-schemas-spoof-site",
                "job_type": "udmi_validation",
                "parameters": {
                    "expected_schedule": {
                        "asset_id": "EM-2",
                        "udmi_version": "nonpub.spoofed",
                    },
                    "state_payload": {
                        "timestamp": "2026-07-14T10:00:00Z",
                        "version": "nonpub.spoofed",
                        "system": {},
                    },
                    "nonpub_schema_sets": {
                        "nonpub.spoofed": {
                            name: json.loads(content) for name, content in _full_set().items()
                        }
                    },
                    "use_live_broker": False,
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        run = self.client.get(f"/api/v1/validation/runs/{run_id}").json()

        self.assertNotIn("nonpub_schema_sets", run["parameters"])
        from app.services.run_service import RunService

        self.assertNotIn("nonpub_schema_sets", RunService().get_run(run_id).parameters)
        descriptions = " ".join(issue["description"] for issue in run["issues"])
        self.assertIn("no schema set with that label has been uploaded", descriptions)


class UdmiSchemaSetChunkedTotalCapTests(ApiTestCase):
    """Authoritative received-bytes TOTAL cap on a chunked upload.

    A chunked request carries no Content-Length header, so the fast pre-check
    cannot fire; the running total over the received files is the authoritative
    limit. MAX_UPLOAD_BYTES is shrunk so each file passes the per-file cap while
    the set total exceeds it.
    """

    env = {**_ENV_OVERRIDES, "MAX_UPLOAD_BYTES": "4096"}
    client_headers = {"X-API-Key": _API_KEY}

    def test_chunked_upload_total_over_cap_is_413(self) -> None:
        filler = json.dumps({"type": "object", "description": "x" * 1800}).encode()
        boundary = "sct-chunked-upload-test"
        parts = [
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="version_label"\r\n\r\n'
                "nonpub.chunked\r\n"
            ).encode()
        ]
        for name in ("state.json", "metadata.json", "events_pointset.json"):
            parts.append(
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                    f'filename="{name}"\r\nContent-Type: application/json\r\n\r\n'
                ).encode()
                + filler
                + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)

        response = self.client.post(
            "/api/v1/udmi/schemas",
            content=iter([body]),  # iterator body => chunked, no Content-Length
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertIn("schema set", response.json()["detail"])
        listed = self.client.get("/api/v1/udmi/schemas")
        self.assertNotIn(
            "nonpub.chunked",
            {entry["version_label"] for entry in listed.json()},
        )


class UdmiSchemaTemplateTests(ApiTestCase):
    """Public downloadable UDMI 1.5.2 schema-set template (GET /template).

    The template is the vendored public-upstream faucetsdn/udmi 1.5.2 schema set
    (Apache-2.0): format documentation with zero project data, so it answers
    without a key even in api_key mode (like GET /imports/templates). The
    round-trip test is load-bearing — it proves the downloaded zip survives the
    strict upload validation, encoding the field workflow (download, edit,
    re-upload under a nonpub label).
    """

    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    def _template_response(self) -> object:
        return self.client.get("/api/v1/udmi/schemas/template")

    def _template_json_members(self) -> dict[str, bytes]:
        response = self._template_response()
        self.assertEqual(response.status_code, 200, response.text)
        members: dict[str, bytes] = {}
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            for name in archive.namelist():
                if name.endswith(".json"):
                    members[name] = archive.read(name)
        return members

    def test_template_is_public(self) -> None:
        from fastapi.testclient import TestClient

        # No auth header in api_key mode still returns the zip — mounted on
        # api_router, not protected_router (regression guard for the button
        # 401ing in hosted mode, as the import templates once did).
        keyless = TestClient(self.app)
        response = keyless.get("/api/v1/udmi/schemas/template")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertIn(
            'filename="udmi-schema-template-1.5.2.zip"',
            response.headers["content-disposition"],
        )
        # Contrast: the sibling protected list route still 401s without a key.
        self.assertEqual(keyless.get("/api/v1/udmi/schemas").status_code, 401)

    def test_template_contents(self) -> None:
        from smart_commissioning_core.udmi_schema import (
            NONPUB_SCHEMA_ROOTS,
            canonical_schema_file_bytes,
        )

        response = self._template_response()
        self.assertEqual(response.status_code, 200, response.text)
        vendored = canonical_schema_file_bytes("1.5.2")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = set(archive.namelist())
            self.assertIn("README.txt", names)
            self.assertIn("LICENSE", names)
            json_members = {name for name in names if name.endswith(".json")}
            # The shipped .json set is exactly the vendored set — no hardcoded
            # file count, no trimming (the $ref closure IS the full set).
            self.assertEqual(json_members, set(vendored))
            for name, raw in vendored.items():
                self.assertEqual(archive.read(name), raw)
        for root in NONPUB_SCHEMA_ROOTS.values():
            self.assertIn(root, json_members)

    def test_template_round_trips_through_upload(self) -> None:
        from smart_commissioning_core.udmi_schema import canonical_schema_file_bytes

        members = self._template_json_members()
        upload = self.client.post(
            "/api/v1/udmi/schemas",
            data={"version_label": "nonpub.template-roundtrip"},
            files=[
                ("files", (name, io.BytesIO(content), "application/json"))
                for name, content in members.items()
            ],
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        # Stored sets outlive this module in the shared per-process database.
        self.addCleanup(
            self.client.delete,
            "/api/v1/udmi/schemas/nonpub.template-roundtrip",
        )
        self.assertEqual(
            set(upload.json()["filenames"]),
            set(canonical_schema_file_bytes("1.5.2")),
        )

    def test_template_bytes_are_stable(self) -> None:
        first = self._template_response()
        second = self._template_response()
        self.assertEqual(first.status_code, 200, first.text)
        # Fixed ZipInfo timestamps => repeated downloads are byte-identical.
        self.assertEqual(first.content, second.content)


if __name__ == "__main__":
    unittest.main()
