"""Register topic-contract helpers shared by the UDMI validation and MQTT
discovery routes.

The ``capture_topics_from_expected`` function encodes the register's topic
conventions (a ``prefix/#`` wildcard covers all three payload types; a single
explicit sibling topic with a blank Payload type implies all three siblings plus
the legacy ``event/pointset`` topic). It was relocated VERBATIM from
``app.api.routes.validation`` so both the UDMI live-capture fan-out and the MQTT
discovery register-comparison derive expected topics through ONE implementation —
raw string matching of a register's ``Expected topic`` would red-flag a device's
metadata topic when its register row lists only ``.../state``.

These are pure functions: no service, repository, or route imports.
"""


def capture_topics_from_expected(expected_topic: object, payload_type: object = None) -> dict:
    """Derive state/metadata/pointset capture topics from a register Expected topic.

    Accepts a ``prefix/#`` wildcard (covers all three), an explicit per-type topic,
    or a comma-separated list of those — matching the register's topic conventions.
    A wildcard also subscribes the legacy singular ``<prefix>/event/pointset`` so
    sites on that convention still deliver their pointset payload.
    """
    topics: dict[str, object] = {}
    extra_topics: list[str] = []
    roots: set[str] = set()
    requested_type = str(payload_type or "").strip().casefold()
    for part in str(expected_topic or "").split(","):
        topic = part.strip()
        if not topic:
            continue
        # Keep a register wildcard in the live subscription set as well as
        # its derived siblings; some site ACLs/brokers behave differently for
        # wildcard versus concrete subscriptions. Explicit topics remain
        # unchanged to avoid broadening their contract.
        if topic.endswith("/#"):
            topics.setdefault("register_topic_filter", topic)
        if topic.endswith("/#"):
            prefix = topic[:-2].rstrip("/")
            roots.add(prefix)
            if requested_type in {"", "state"}:
                topics.setdefault("state_topic", prefix + "/state")
            if requested_type in {"", "metadata"}:
                topics.setdefault("metadata_topic", prefix + "/metadata")
            if requested_type in {"", "pointset"}:
                topics.setdefault("pointset_topic", prefix + "/events/pointset")
                extra_topics.append(prefix + "/event/pointset")
        elif topic.endswith("/state") and requested_type in {"", "state"}:
            roots.add(topic.removesuffix("/state"))
            topics["state_topic"] = topic
        elif topic.endswith("/metadata") and requested_type in {"", "metadata"}:
            roots.add(topic.removesuffix("/metadata"))
            topics["metadata_topic"] = topic
        elif topic.endswith("/events/pointset") and requested_type in {"", "pointset"}:
            roots.add(topic.removesuffix("/events/pointset"))
            topics["pointset_topic"] = topic
        elif topic.endswith("/event/pointset") and requested_type in {"", "pointset"}:
            roots.add(topic.removesuffix("/event/pointset"))
            topics["pointset_topic"] = topic

    # field engineer's register contract: blank Payload type represents one WHOLE asset,
    # so even one explicit sibling topic must require all three payload slots.
    required_slots = {"state_topic", "metadata_topic", "pointset_topic"}
    if not requested_type and roots and not required_slots.issubset(topics):
        if len(roots) == 1:
            prefix = next(iter(roots))
            topics.setdefault("state_topic", prefix + "/state")
            topics.setdefault("metadata_topic", prefix + "/metadata")
            topics.setdefault("pointset_topic", prefix + "/events/pointset")
            extra_topics.append(prefix + "/event/pointset")
    if extra_topics:
        topics["extra_capture_topics"] = list(dict.fromkeys(extra_topics))
    return topics


def expected_topic_filters(rows: list[dict]) -> list[tuple[str, str]]:
    """(asset_identity, topic_filter) pairs for every accepted mqtt_register row.

    Each register row is expanded through :func:`capture_topics_from_expected`
    so the register's topic contract (blank Payload type = whole asset = all
    three sibling topics + legacy event/pointset) is honoured through the ONE
    canonical implementation rather than raw string matching.

    A row that carries a ``/#`` wildcard (``register_topic_filter``) contributes
    ONLY that wildcard: it already covers every child topic, and citing it as the
    match keeps the "one wildcard green-lights many topics" semantics visible to
    the operator (the derived concrete siblings would otherwise mis-cite a
    wildcard-covered topic as individually listed and would pollute the
    unobserved-filters count with children the wildcard already covers). A row
    with no wildcard contributes its concrete sibling filters (state, metadata,
    pointset, and the legacy event/pointset), so a blank-Payload-type row still
    expects all three siblings.

    Filters are de-duplicated on first-seen across all rows in register order, so
    overlapping rows attribute to the earliest one (the green/red verdict is
    identical either way; only the cited asset/filter differs).
    """
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = str(row.get("Asset ID") or row.get("Asset name") or "").strip()
        topics = capture_topics_from_expected(row.get("Expected topic"), row.get("Payload type"))
        wildcard = topics.get("register_topic_filter")
        if wildcard:
            ordered: list[str] = [str(wildcard)]
        else:
            ordered = []
            for slot in ("state_topic", "metadata_topic", "pointset_topic"):
                value = topics.get(slot)
                if value:
                    ordered.append(str(value))
            for extra in topics.get("extra_capture_topics") or []:
                if extra:
                    ordered.append(str(extra))
        for topic_filter in ordered:
            if topic_filter and topic_filter not in seen:
                seen.add(topic_filter)
                result.append((identity, topic_filter))
    return result
