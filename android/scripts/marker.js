const markerUrl = typeof CAPTURE_MARKER_URL === "undefined" ? "" : CAPTURE_MARKER_URL;
if (markerUrl) {
  const stepId = typeof STEP_ID === "undefined" ? "" : STEP_ID;
  if (!stepId) {
    throw new Error("STEP_ID was not supplied for this capture marker");
  }
  const body = JSON.stringify({
    step_id: stepId,
    label: typeof STEP_LABEL === "undefined" ? stepId : STEP_LABEL,
    action_kind: typeof ACTION_KIND === "undefined" ? "tap" : ACTION_KIND,
    action_target: typeof ACTION_TARGET === "undefined" ? "" : ACTION_TARGET
  });
  const response = http.post(`${markerUrl}/marker`, {
    headers: { "Content-Type": "application/json" },
    body
  });
  if (response.status < 200 || response.status >= 300) {
    throw new Error(`Capture marker collector returned HTTP ${response.status}`);
  }
}
