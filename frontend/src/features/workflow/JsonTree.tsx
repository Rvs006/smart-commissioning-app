/**
 * The JSON payload explorer, shared by the UDMI/MQTT inspectors in ModulePage
 * and the native MQTT scanner's side panel. It was private to ModulePage, which
 * is why the scanner rebuild lost the tree; keeping it here means one renderer
 * for every payload view.
 */
export function JsonTree({ value }: { value: unknown }) {
  if (value === null || typeof value !== "object") {
    return <span>{JSON.stringify(value)}</span>;
  }
  return (
    <ul className="json-tree">
      {Object.entries(value).map(([key, child]) => (
        <li key={key}>
          {child !== null && typeof child === "object" ? (
            <details>
              <summary>{key}</summary>
              <JsonTree value={child} />
            </details>
          ) : (
            <>
              <strong>{key}</strong>: {JSON.stringify(child)}
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

/**
 * The engine wraps a JSON scalar or list under `_value`, and records a non-JSON
 * payload as a presence marker rather than raw bytes. Unwrap the first, and
 * report the second honestly instead of rendering an empty tree.
 */
function unwrapObservedPayload(payload: unknown): {
  value: unknown;
  rawOnly: boolean;
} {
  const isObject = payload !== null && typeof payload === "object";
  if (!isObject) {
    return { value: payload, rawOnly: false };
  }
  const record = payload as Record<string, unknown>;
  return {
    value: "_value" in record ? record._value : payload,
    rawOnly: record._raw_present === true,
  };
}

/**
 * Mirrors the UDMI observed-payload block (pre + Explore JSON tree). Honesty: a
 * non-JSON payload is stored as a presence marker, so we say exactly that and
 * render no tree.
 */
export function MqttPayloadPanel({
  payload,
  topicName,
}: {
  payload: unknown;
  topicName: string;
}) {
  const { rawOnly, value } = unwrapObservedPayload(payload);
  return (
    <div className="payload-inspector">
      <h4>Last payload on {topicName}</h4>
      {rawOnly ? (
        <p className="section-copy">
          Non-JSON payload observed. The engine stores a presence marker, not the raw bytes.
        </p>
      ) : (
        <>
          <pre className="payload-cell">{JSON.stringify(value, null, 2)}</pre>
          <details className="json-inspector">
            <summary>Explore JSON tree</summary>
            <JsonTree value={value} />
          </details>
        </>
      )}
    </div>
  );
}
