# Vendored UDMI schemas

The JSON files under `1.5.2/` are an unmodified recursive `$ref` closure for
`state.json`, `metadata.json`, and `events_pointset.json` from the official
[`faucetsdn/udmi` `1.5.2` tag](https://github.com/faucetsdn/udmi/tree/1.5.2/schema),
commit `7e85ec01c1a0e5a3506543ec8a6a4b2b46e498f6`.

Only the 34 files reachable from those three payload roots are included. The
adjacent `LICENSE` is the upstream Apache-2.0 license.
