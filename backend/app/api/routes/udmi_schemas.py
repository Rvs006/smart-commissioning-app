"""Operator-uploaded non-published UDMI schema sets (``nonpub.*`` versions).

Some projects deliberately do not conform to any published UDMI version (field
ask 2026-07-14). An engineer uploads the project's Draft 7 schema set here —
the same state.json / metadata.json / events_pointset.json root layout as the
vendored spec, plus any files their ``file:...`` $refs reach — under a nonpub
version label. UDMI run creation (routes/validation.py) embeds every stored
set into the run parameters, so the inline path and the Dramatiq worker (which
shares only the database) validate declared-nonpub payloads identically.
"""

import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError
from smart_commissioning_core.db.repositories import UdmiSchemaSetRepository
from smart_commissioning_core.rbac import Role
from smart_commissioning_core.udmi_schema import (
    NONPUB_SCHEMA_ROOTS,
    is_nonpub_version,
    nonpub_version_key,
)

from app.api.uploads import check_content_length, read_upload_capped, upload_too_large
from app.core.auth import AuthPrincipal, require_role
from app.core.config import get_settings
from app.core.db import get_engine
from app.schemas.udmi_schemas import UdmiSchemaSetSummary

router = APIRouter()

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
