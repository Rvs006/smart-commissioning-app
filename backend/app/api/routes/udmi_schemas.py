"""Operator-uploaded non-published UDMI schema sets (``nonpub.*`` versions).

Some projects deliberately do not conform to any published UDMI version (field
ask 2026-07-14). An engineer uploads the project's Draft 7 schema set here —
the same state.json / metadata.json / events_pointset.json root layout as the
vendored spec, plus any files their ``file:...`` $refs reach — under a nonpub
version label. UDMI run creation (routes/validation.py) embeds every stored
set into the run parameters, so the inline path and the Dramatiq worker (which
shares only the database) validate declared-nonpub payloads identically.
"""

import io
import json
import re
import zipfile
from functools import cache
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError
from smart_commissioning_core.db.repositories import UdmiSchemaSetRepository
from smart_commissioning_core.rbac import Role
from smart_commissioning_core.udmi_schema import (
    NONPUB_SCHEMA_ROOTS,
    canonical_schema_file_bytes,
    is_nonpub_version,
    nonpub_version_key,
    schema_license_bytes,
)

from app.api.uploads import check_content_length, read_upload_capped, upload_too_large
from app.core.auth import AuthPrincipal, require_role
from app.core.config import get_settings
from app.core.db import get_engine
from app.schemas.udmi_schemas import UdmiSchemaSetSummary

router = APIRouter()

# RBAC posture (mirrors imports.public_router in routes/imports.py): the schema
# template is the vendored public-upstream faucetsdn/udmi 1.5.2 schema set
# (Apache-2.0) — format documentation carrying zero project data — so it is
# served unauthenticated like GET /imports/templates. Registered on api_router
# (not protected_router) in api/router.py so the download button never 401s in
# hosted api_key mode.
public_router = APIRouter()

# 1.5.2 is the only vendored published version. ponytail: if another UDMI
# version is vendored, this constant and the frontend filename literal both need
# a touch (getUdmiSchemaTemplatePath / the download call in ModulePage.tsx).
_TEMPLATE_VERSION = "1.5.2"
_TEMPLATE_FILENAME = f"udmi-schema-template-{_TEMPLATE_VERSION}.zip"

_TEMPLATE_README = """\
UDMI 1.5.2 schema-set template
==============================

This zip is the complete, published UDMI 1.5.2 Draft-7 JSON Schema set:
the three roots state.json / metadata.json / events_pointset.json plus their
full "file:" $ref closure. It is vendored verbatim from
github.com/faucetsdn/udmi tag 1.5.2 and redistributed under the Apache-2.0
license included here as LICENSE.

Use it to build a NON-PUBLISHED (nonpub.*) schema set for a project that
deliberately deviates from published UDMI:

1. Extract the .json files and edit them to match your project's payloads
   (for a quick smoke test, add a required property to state.json).
2. On the UDMI Validation page, Non-Published UDMI Schema Sets section, upload
   ALL of the .json files together under a label that starts with "nonpub"
   (e.g. nonpub.1 or nonpub-siteA).
3. Have your payloads declare that same version string so validation runs
   against your uploaded set instead of a published UDMI release.

Upload only the .json files. README.txt and LICENSE are not schemas and the
upload rejects any non-.json file.

The upload enforces:
  - all three root files (state.json, metadata.json, events_pointset.json);
  - every "file:<name>.json" $ref must resolve within the files you upload;
  - each file is a valid Draft 7 JSON Schema;
  - at most 64 files per set;
  - a 2 MB combined ceiling across all stored sets.

Because these rules require the whole $ref closure, keep the full set together:
the closure of the three roots IS every .json file in this zip.
"""

# RBAC: seeing which sets exist is viewer+; uploading or deleting a set changes
# what every FUTURE UDMI run validates against, so it is engineer+.
require_viewer = require_role(Role.VIEWER)
require_engineer = require_role(Role.ENGINEER)

_REQUIRED_ROOTS = tuple(NONPUB_SCHEMA_ROOTS.values())
# The stored label column is String(255) (core.db.models.UdmiSchemaSet).
_MAX_LABEL_LENGTH = 255
# A set is the three roots plus whatever their $refs reach; 64 files is far
# beyond any real schema set while keeping the per-request work bounded.
_MAX_SCHEMA_FILES = 64
# ponytail: 2 MB ceiling on the combined serialized size of ALL stored sets.
# Every UDMI run embeds every stored set into its own parameters row (the
# queued worker shares only the database), so the stored total — not just a
# single upload — is what bounds per-run row growth.
_MAX_TOTAL_STORED_SCHEMA_BYTES = 2 * 1024 * 1024


def _repository() -> UdmiSchemaSetRepository:
    return UdmiSchemaSetRepository(get_engine())


def _validated_label(version_label: str) -> str:
    """The stripped label, or 400 when it is not a safe nonpub label.

    Same sanitisation as ConfigurationService._secret_path: the label is an
    operator-typed lookup key and must never smuggle path syntax.
    """
    label = version_label.strip()
    if not label or "/" in label or "\\" in label or ".." in label:
        raise HTTPException(
            status_code=400,
            detail=(
                "version_label must be a plain label without path separators "
                "or '..' — e.g. 'nonpub.1'."
            ),
        )
    if not is_nonpub_version(label):
        raise HTTPException(
            status_code=400,
            detail=(
                f"version_label '{label}' is not a non-published version label. "
                "Use 'nonpub' optionally followed by a suffix (e.g. 'nonpub.1', "
                "'nonpub-siteA'); published UDMI versions use the vendored schemas."
            ),
        )
    if len(nonpub_version_key(label)) > _MAX_LABEL_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=(
                f"version_label is {len(nonpub_version_key(label))} characters long; "
                f"the stored label may be at most {_MAX_LABEL_LENGTH}."
            ),
        )
    return label


async def _parse_schema_files(
    request: Request, files: list[UploadFile]
) -> dict[str, dict]:
    """Size-capped, JSON-parsed, Draft-7-checked ``{filename: schema}``; else 400/413."""
    settings = get_settings()
    check_content_length(request, settings.max_upload_bytes, noun="schema file")
    if len(files) > _MAX_SCHEMA_FILES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Schema set has {len(files)} files; a set may contain at most "
                f"{_MAX_SCHEMA_FILES}."
            ),
        )
    schemas: dict[str, dict] = {}
    total_bytes = 0
    for file in files:
        name = Path(file.filename or "").name
        if not name or not name.endswith(".json"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Schema file '{file.filename or ''}' must be a .json file "
                    "(e.g. state.json)."
                ),
            )
        if name in schemas:
            raise HTTPException(
                status_code=400,
                detail=f"Schema file '{name}' was uploaded more than once in this set.",
            )
        raw = await read_upload_capped(file, settings.max_upload_bytes, noun="schema file")
        # Authoritative TOTAL cap on the bytes actually received: the per-file
        # cap above multiplies by file count, and the Content-Length pre-check
        # is advisory only (a chunked request carries no Content-Length).
        total_bytes += len(raw)
        if total_bytes > settings.max_upload_bytes:
            raise upload_too_large(settings.max_upload_bytes, noun="schema set")
        try:
            schema = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HTTPException(
                status_code=400,
                detail=f"Schema file '{name}' is not valid JSON: {error}",
            ) from error
        if not isinstance(schema, dict):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Schema file '{name}' must contain a JSON object (a Draft 7 "
                    f"schema), not {type(schema).__name__}."
                ),
            )
        try:
            Draft7Validator.check_schema(schema)
        except SchemaError as error:
            raise HTTPException(
                status_code=400,
                detail=f"Schema file '{name}' is not a valid Draft 7 JSON Schema: {error.message}",
            ) from error
        schemas[name] = schema
    return schemas


def _require_root_files(schemas: dict[str, dict]) -> None:
    missing = [root for root in _REQUIRED_ROOTS if root not in schemas]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Schema set is missing required root file(s): {', '.join(missing)}. "
                f"A set must include all of: {', '.join(_REQUIRED_ROOTS)}."
            ),
        )


def _refs(node: object) -> set[str]:
    """Every string ``$ref`` value anywhere in a schema."""
    refs: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                refs.add(value)
            refs.update(_refs(value))
    elif isinstance(node, list):
        for item in node:
            refs.update(_refs(item))
    return refs


# The only external-$ref form the run-time resolver can reach: the validator's
# registry maps bare ``file:<name>`` URIs onto the uploaded set (see core
# udmi_schema._uploaded_schema_findings) — no filesystem, no network.
_SUPPORTED_FILE_REF = re.compile(r"^file:[^#/\\]+\.json(#.*)?$")


def _require_supported_refs(schemas: dict[str, dict]) -> None:
    """400 for any $ref form the run-time resolver cannot reach.

    Same-document refs (``#/...``) and the ``file:#...`` self-reference form
    resolve without the registry; every other ref must be
    ``file:<name>.json`` (optionally ``#<fragment>``). A plain-relative,
    http(s), or urn ref passes Draft 7 well-formedness but explodes as an
    unresolvable (or, worse, fetching) reference at validation time — reject
    it at upload time instead, naming the offending ref.
    """
    for filename in sorted(schemas):
        for ref in sorted(_refs(schemas[filename])):
            if ref.startswith("#") or ref.startswith("file:#"):
                continue
            if not _SUPPORTED_FILE_REF.match(ref):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Schema file '{filename}' has an unsupported $ref '{ref}'. "
                        "$refs must be same-document ('#/...') or reference a file "
                        "uploaded in this set as 'file:<name>.json' (optionally "
                        "followed by '#<fragment>')."
                    ),
                )


def _require_refs_resolve(schemas: dict[str, dict]) -> None:
    """400 when any ``file:`` $ref points outside the uploaded set.

    The validator resolves refs only against the set itself (no filesystem, no
    network), so a dangling ref would surface as a run-time resolution error —
    reject it at upload time instead, naming the missing file(s).
    """
    file_refs = {
        ref.removeprefix("file:").split("#", 1)[0]
        for schema in schemas.values()
        for ref in _refs(schema)
        if ref.startswith("file:") and not ref.startswith("file:#")
    }
    dangling = sorted(file_refs - set(schemas))
    if dangling:
        raise HTTPException(
            status_code=400,
            detail=(
                "Schema set has $refs to file(s) not included in this upload: "
                f"{', '.join(dangling)}. Upload the complete set in one request."
            ),
        )


def _require_stored_total_within_cap(
    repository: UdmiSchemaSetRepository, version_label: str, schemas: dict[str, dict]
) -> None:
    """413 when storing this set would push ALL stored sets past the ceiling."""
    stored = repository.get_all_files()
    # Upsert semantics: a re-upload replaces its own label, it does not add.
    stored[nonpub_version_key(version_label)] = schemas
    total = sum(
        len(json.dumps(files, separators=(",", ":")).encode("utf-8"))
        for files in stored.values()
    )
    if total > _MAX_TOTAL_STORED_SCHEMA_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Storing this set would bring all stored UDMI schema sets to {total} "
                f"bytes combined, above the {_MAX_TOTAL_STORED_SCHEMA_BYTES}-byte ceiling. "
                "Delete an unused set or trim this one."
            ),
        )


@router.post("", response_model=UdmiSchemaSetSummary)
async def upload_udmi_schema_set(
    request: Request,
    version_label: str = Form(...),
    files: list[UploadFile] = File(...),
    principal: AuthPrincipal = Depends(require_engineer),
) -> UdmiSchemaSetSummary:
    label = _validated_label(version_label)
    schemas = await _parse_schema_files(request, files)
    _require_root_files(schemas)
    _require_supported_refs(schemas)
    _require_refs_resolve(schemas)
    repository = _repository()
    _require_stored_total_within_cap(repository, label, schemas)
    summary = repository.upsert_set(
        version_label=label, files=schemas, uploaded_by=principal.username
    )
    return UdmiSchemaSetSummary.model_validate(summary)


@router.get(
    "",
    response_model=list[UdmiSchemaSetSummary],
    dependencies=[Depends(require_viewer)],
)
def list_udmi_schema_sets() -> list[UdmiSchemaSetSummary]:
    return [
        UdmiSchemaSetSummary.model_validate(summary)
        for summary in _repository().list_sets()
    ]


@router.delete(
    "/{version_label}",
    status_code=204,
    dependencies=[Depends(require_engineer)],
)
def delete_udmi_schema_set(version_label: str) -> None:
    if not _repository().delete_set(version_label):
        raise HTTPException(
            status_code=404,
            detail=f"UDMI schema set '{version_label}' was not found.",
        )


@cache
def _template_zip_bytes() -> bytes:
    """The downloadable UDMI 1.5.2 template zip (README + LICENSE + all schemas).

    Ships the FULL vendored set: the $ref closure of the three roots IS every
    vendored .json file (verified 2026-07-16: 34 files, 40,879 raw bytes ~ 2%
    of _MAX_TOTAL_STORED_SCHEMA_BYTES when re-uploaded), so nothing is trimmable
    without breaking _require_refs_resolve on re-upload. Cached because the
    vendored files are immutable per release; combined with fixed ZipInfo
    timestamps every download is byte-identical.
    """
    # Fixed timestamp (the zip epoch floor) so repeated downloads byte-match:
    # ZipFile.writestr with a str name would stamp wall-clock time instead.
    fixed_date = (1980, 1, 1, 0, 0, 0)
    members: list[tuple[str, bytes]] = [
        ("README.txt", _TEMPLATE_README.encode("utf-8")),
        ("LICENSE", schema_license_bytes()),
    ]
    members.extend(sorted(canonical_schema_file_bytes(_TEMPLATE_VERSION).items()))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members:
            info = zipfile.ZipInfo(filename=name, date_time=fixed_date)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return buffer.getvalue()


@public_router.get("/template")
def download_udmi_schema_template() -> Response:
    return Response(
        content=_template_zip_bytes(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{_TEMPLATE_FILENAME}"',
        },
    )
