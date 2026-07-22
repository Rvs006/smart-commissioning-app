"""Fail when known private site or personnel tokens enter tracked text files.

Only SHA-256 fingerprints are stored here, so the denylisted values themselves
do not become another copy in the public repository. URL decoding is applied
twice to catch ordinary and double-percent-encoded residue.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

# Only one-way fingerprints are committed. The first set covers personnel and
# site fragments that occur as alphanumeric tokens. The second covers complete
# hyphenated device IDs and slash-delimited topic prefixes.
_WORD_HASHES = frozenset(
    {
        "6cb912de5b6ef87cb3ecf92c13052d80d02ed91771e6e640625d759cd444e941",
        "c0e7aec81a1a194e9f54f6b297f6544041188c741ae8f55c2133b5c510a7dc1d",
        "2c3827a002d7a48cd3ad594495f7a3c218094f2a58a81d39c9e513531e914ad5",
        "973d7f9efc06ba81112bceb8205309ff0f49c303c28914a1dcf143d86a1b15b4",
        "47d22761ec645b58a8c27dfdf94f9f6e84cf24eb0c334b8b7b8a54b150cdd78d",
        "0ca1a53ca7045188e1a5386898d3479aaa4275910ad9eda607e3d45d8ff93d91",
        "8102f9b8d129ab1e276471effb10f0b0d099d2a5b8aafae176240869de106e33",
        "f76cb816b3f74ecf30d387c64869038ac163fe26f8aabd727c1071dd567fc3d5",
        "69c04ffd98674f1d86cf0460bc1bb17d41512763afbdabdd44232e1bb30629f1",
    }
)
_STRUCTURED_HASHES = frozenset(
    {
        "97f085c6e05f07f5833869fb49c3a54bd618273dbc08031ca393ce98f8b36e73",
        "cddf184c5a71f894785c19bd8e38a6eed1342871ae3e214b50d9e1dc2bf8a542",
        "bc15590cb3598dfa92f58be39872bd966953effd98f615bfbfe260fc49a3e5a2",
        "cecb87ffe284378d084737f712d177191b9d2c6b1b1ce6c7f251fa454d717237",
        "0ee3917eb857440d436c1bddc6a07533bfe8bbbb20648f261bc50e254087fef1",
    }
)
_NETWORK_PREFIX_HASHES = frozenset(
    {"c239d59dc64fde2097ac5cba6beb52ad3904ff8625423fb422de4fd4622eb578"}
)
_HOST_HASHES = frozenset(
    {"f4daeead061bb5517622701bf000ec57f1612e65a8315916ac5b170effe676b4"}
)
_MAC_HASHES = frozenset(
    {
        "554f90db1c7ee5fb3635bda366b2a3eed6557d87edaf58b946f73ec614bc110f",
        "889eccd00a07e4933b89081699853a39f94f3442c892261f740c6298c2b8022c",
        "7936c757f36e09471822260e7c93396795d4816caae0ac2f255511dd925e6176",
        "2e2b105430a98220911b93da261c2eeca5b923cb3e757bc48f6d3c7ad119a4dc",
    }
)
_WORD_RE = re.compile(r"[a-z0-9]+")
_STRUCTURED_RE = re.compile(r"[a-z0-9]+(?:[-_/][a-z0-9]+)+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HOST_RE = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\b")
_MAC_RE = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"])
    return [Path(value.decode("utf-8")) for value in output.split(b"\0") if value]


def _matches(text: str) -> bool:
    lowered = text.casefold()
    if any(_digest(match.group()) in _WORD_HASHES for match in _WORD_RE.finditer(lowered)):
        return True
    if any(
        _digest(".".join(match.group().split(".")[:3])) in _NETWORK_PREFIX_HASHES
        for match in _IPV4_RE.finditer(lowered)
    ):
        return True
    if any(_digest(match.group()) in _HOST_HASHES for match in _HOST_RE.finditer(lowered)):
        return True
    if any(_digest(match.group()) in _MAC_HASHES for match in _MAC_RE.finditer(lowered)):
        return True
    for match in _STRUCTURED_RE.finditer(lowered):
        candidate = match.group()
        delimiter = "/" if "/" in candidate else "_" if "_" in candidate else "-"
        segments = candidate.split(delimiter)
        # Check contiguous subpaths too, so a known topic nested under a scheme
        # or a known device identifier followed by a suffix is still rejected.
        for start in range(len(segments)):
            for end in range(start + 2, min(len(segments), start + 8) + 1):
                if _digest(delimiter.join(segments[start:end])) in _STRUCTURED_HASHES:
                    return True
    return False


def main() -> int:
    violations: list[str] = []
    for path in _tracked_files():
        if _matches(path.as_posix()):
            violations.append(f"{path}: denied token in path")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        decoded = unquote(unquote(text))
        if _matches(decoded):
            violations.append(f"{path}: denied token in content")
    if violations:
        print("Public-repository hygiene check failed:")
        print("\n".join(f"- {item}" for item in violations))
        return 1
    print("Public-repository hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
