# v0.1.43 - Nmap one-click approval

v0.1.43 moves Nmap deployment authority out of Configuration. In the one
Windows portable EXE, a global administrator opens IP Scanner and approves the
one detected signed local Nmap installation. Engineers then select the approved
TCP-connect scanner without entering publisher, version, hash, licence, path,
or script details.

The recorded approval lives in the stable local app data, so it survives a
portable upgrade. A changed Nmap installation or executor identity disables the
approval until a global administrator reviews it again. There is one portable
EXE for every release audience. It does not include, download, or install Nmap
or Npcap, and it does not expose the separate XML-import endpoint. Docker and
Linux keep their existing disabled-by-default Nmap provider setting.

## Validation boundary

Automated checks prove the approval and packaging contracts. They do not prove
that a particular customer network is suitable for scanning. Obtain the normal
site authorization and keep real addresses, devices, and operator details in
the private commissioning record.

## Release artifacts

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`
- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

Use the [migration rollback guide](migration-rollback-v0.1.43.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.43.md), and
[release validation record](release-validation-v0.1.43.md).
