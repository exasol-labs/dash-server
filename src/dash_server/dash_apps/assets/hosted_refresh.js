/*
 * Hosted-app auto-refresh clientside callback.
 *
 * Registered by `dash_server.dash_apps.branding.apply_hosted_footer` as the
 * body of a Dash clientside callback. It polls the app's `/__dash-server/status`
 * route and reloads the page when the served revision changes, so a
 * promote/rollback is reflected without a manual refresh.
 *
 * Inputs (bound in branding.py, keyed by the `__dash-server` element ids in the
 * branding constants block):
 *   nIntervals - dcc.Interval n_intervals (poll tick)
 *   meta       - dcc.Store data: {mount_path: string, revision_number: number}
 */
function (nIntervals, meta) {
  if (!meta || typeof meta.mount_path !== "string" || typeof meta.revision_number !== "number") {
    return "";
  }
  var statusUrl = meta.mount_path.replace(/\/$/, "") + "/__dash-server/status";
  fetch(statusUrl, {credentials: "same-origin", cache: "no-store"})
    .then(function (response) {
      if (!response.ok) {
        return null;
      }
      return response.json();
    })
    .then(function (payload) {
      if (!payload || typeof payload.revision_number !== "number") {
        return;
      }
      if (payload.revision_number !== meta.revision_number) {
        window.location.reload();
      }
    })
    .catch(function () {
      return null;
    });
  return "";
}
