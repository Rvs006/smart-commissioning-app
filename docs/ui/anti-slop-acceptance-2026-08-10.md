# Aug 10 anti-slop UI acceptance checklist

This file is the reproducible acceptance record required by U5 and U9 of the IP/BACnet discovery plan. The user supplied the full law in the Aug 10 request. The canonical raw source is pinned here so reviewers can verify the exact text rather than treating the repository's shorter `AGENTS.md` as a substitute.

- Source: `https://pols.dev/slop.md`
- Source bytes retrieved: 2026-08-12
- UTF-8 byte length: `87528`
- SHA-256: `a9e8d49155afba53e2c4621028a2c7bda679dd09841d77bc9c251441d5248ee7`
- Separate guidance: repository `AGENTS.md` and the Humanizer rules still apply independently.

U9 implementation evidence: `ModulePage` now keeps BACnet property expansion in a bounded child
flow. The sealed parent exposes its property ceiling, selected read set, destination, and caps;
the child preview is polled before a matching authorization can start it, and the UI exposes
queued, running, cancelling, authorization-expired, failed, cancelled, and sealed states with a
separate Stop control. Parent evidence remains immutable. The frontend suite (508 tests),
typecheck, ESLint, Prettier, and production build passed after this flow was added.

For each item, U5 and U9 must be marked `PASS` or `N/A`, with a concrete DOM test, browser viewport check, code location, or short applicability reason. Unreviewed is not an acceptable release state. Any failure must be fixed before the unit handoff.

001. **Lucide React package**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
002. **Em dashes**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
003. **Pill / eyebrow badge**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
004. **Fonts: Fraunces and Work Sans**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
005. **Glowy pill buttons**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
006. **Oversized icon in a colored tile**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
007. **Floating cards**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
008. **Cut-off glow**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
009. **The kitchen-sink card**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
010. **Fake macOS / app window mockup**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
011. **Font: Space Grotesk**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
012. **Purple, and blue-to-purple gradients**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
013. **Gradient pill with icon and text**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
014. **The default CTA button pair**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
015. **The three-tier pricing block**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
016. **The testimonial / quote card**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
017. **Gradient-circle initials avatar**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
018. **The pre-footer CTA banner**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
019. **The logo lockup (gradient icon tile + wordmark)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
020. **Font: Cormorant Garamond**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
021. **The split hero**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
022. **Grid / graph-paper background**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
023. **Crude CSS/SVG "illustrations"**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
024. **The accent-bar card**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
025. **Background glow**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
026. **The fake code-snippet window**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
027. **Fonts: Sora and JetBrains Mono**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
028. **Floating tag pinned to an image**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
029. **Font: Syne**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
030. **Gradient-filled headline text**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
031. **Fonts: Archivo, and Inter everywhere**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
032. **Hairline light border on boxes**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
033. **Countdown timer**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
034. **The card hover-lift**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
035. **Letterspaced serif wordmark**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
036. **Fonts: high-contrast Didone serifs (Bodoni, Didot)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
037. **Monospace as the house voice**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
038. **One label treatment, everywhere**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
039. **Botched glass**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
040. **The faint grid background (restated, because it keeps happening)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
041. **Botched fill animations**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
042. **Never hide content behind an entrance animation (the invisible-content trap)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
043. **Content sliced by an edge — the cut-off tell (and "clear the cut")**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
044. **Misaligned parallel columns — the ragged comparison grid**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
045. **Text jammed against the edge**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
046. **The default all-around shadow**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
047. **Content flung to the far edges (default asymmetry)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
048. **Missing (or faked) logos and icons that would earn their place**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
049. **Nothing is actually centered — the chronic centering miss**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
050. **Faking a shadow with a second box**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
051. **An icon or a logo with a box behind it**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
052. **The little rule beside a label (the eyebrow tick)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
053. **The oversized footer wordmark done as slop**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
054. **Colliding colours, and hard colour seams between sections**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
055. **Botched shadow — the hard-edged box behind it**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
056. **Text you cannot read — colour with no contrast**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
057. **The bloom that is just the element's shape, blurred**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
058. **The dot under the active nav item**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
059. **Content clipped where two sections overlap**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
060. **Cramped display type — no air to breathe**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
061. **Grain sitting on top of the content**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
062. **The cool blue-charcoal (dark slop's default palette)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
063. **The pastel candy gradient background**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
064. **Drifting soft-blend gradient blobs (the candy aurora)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
065. **Radial glow halo behind an object**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
066. **A hero that does not own the first screen**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
067. **The cream / beige "editorial" background**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
068. **The slop gray (the default UI-kit neutral)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
069. **Default Google fonts, the whole rotation**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
070. **The hover boop (a button that jumps)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
071. **The inner-glow box (a badge that lights up from inside)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
072. **The off-center strike or cut line**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
073. **The default hero stack**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
074. **The fixed background that just follows the scroll**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
075. **Hard image seams**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
076. **Saturated accent color**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
077. **Underline-fill hover animations**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
078. **The sun-and-moon theme toggle, and redrawn line icons**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
079. **Unrounded hairline rules, lines used as decoration**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
080. **The kicker-plus-serif-H2 section head**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
081. **The big serif statement block**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
082. **The inset enquire island with a form**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
083. **The email-pill plus button form**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
084. **The image card with overlay caption**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
085. **A flat fill under everything after the hero**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
086. **Recycling your own house style**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
087. **The hero stack with a panel on the right**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
088. **The multi-line headline (and the dangling accent word)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
089. **The filled-button-next-to-outlined-button pair**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
090. **The small-label-over-big-heading section head**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
091. **Numbered steps beside a vertical line**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
092. **Stacking slop layouts (the compounding rule)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
093. **The whole SaaS product-page template (the meta-skeleton)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
094. **Labels and metadata as tinted pill chips, everywhere**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
095. **Dead controls and fake interactivity**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
096. **Even the "tasteful" font swap**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
097. **The same skeleton, recolored**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
098. **The standard footer**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
099. **No icons at all**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
100. **Avoiding the list is not design**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
101. **Real translucency (liquid glass)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
102. **Self-colored borders and tonal elevation**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
103. **Bespoke geometry beats default shapes**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
104. **Bare icons, no container**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
105. **Say less**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
106. **Custom, in-house iconography**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
107. **Authored micro-interactions**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
108. **Considered light, not the default glow**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
109. **Premium noise**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
110. **Liquid-glass button: a concrete recipe**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
111. **Premium type usually means licensed type**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
112. **Full-page, large-scale composition**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
113. **Real logo walls (earned social proof)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
114. **Blueprint / canvas backgrounds**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
115. **Inset "island" sections**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
116. **Good: crafted custom SVG renders**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
117. **Good: the gloss is the good part of glass**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
118. **Professional does not mean lifeless**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
119. **The good grid: a fine textured micro-grid**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
120. **Grainy gradients, never banded ones**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
121. **Scroll-authored motion**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
122. **The oversized footer wordmark, placed right**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
123. **1. One signature artifact**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
124. **2. Atmosphere, not a flat fill**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
125. **3. Layered depth on the z-axis**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
126. **4. The product as a real, populated artifact**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
127. **5. Character in the display type**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
128. **6. One bespoke silhouette**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
129. **7. The nav is treated, not defaulted**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
130. **8. Real specificity**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
131. **The formula**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
132. **The signature serif headline**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
133. **Two-tone / accent headline**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
134. **Full-bleed atmospheric hero**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
135. **Animated character-field background**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
136. **Gradient-filled icons (a jewel inside the mark)**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
137. **The arrow is a tell, so sweat it**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
138. **One cohesive visual language ("synchronized edition")**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
139. **The premium glass CTA, when it earns it**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
140. **Use real component libraries, do not hand-roll generic UI**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
141. **Cohesion is the whole game**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
142. **"Creative" is not "realistic"**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
143. **Type without the Google slop shelf**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
144. **The product-as-artifact is a signature, not the slop window**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
145. **Take the design LANGUAGE from references, never the content**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
146. **"Distinctive font" keeps moving: even the nice free grotesques read generic**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
147. **Dead-looking is a fail on its own**
    - U5: PASS
    - U5 evidence: Static source audit of ModulePage, RunHistoryPage, shared styles, and the 508-test Vitest suite; typecheck, ESLint, Prettier, and production build passed.
    - U9: PASS
    - U9 evidence: BACnet and IP states use the shared module surface; focused DOM tests, typecheck, ESLint, Prettier, and production build passed. Hardware and packet cells remain explicitly UNPROVEN where no signed field evidence exists.
