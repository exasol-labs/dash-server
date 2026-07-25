/*
 * Browser session channel: evaluate ephemeral agent JavaScript in a live tab.
 *
 * Registered by `dash_server.dash_apps.branding.apply_hosted_footer` as the body of
 * a Dash clientside callback, and only in local mode (see the three-point gate in
 * plans/live-dashboard-introspection-plan.md). The callback is driven by a
 * `dcc.Interval`; each tick polls the control plane for one pending command, runs
 * it, and posts a bounded result back.
 *
 * Inputs (bound in branding.py, keyed by the `__dash-server` element ids there):
 *   nIntervals - dcc.Interval n_intervals (poll tick)
 *   meta       - dcc.Store data: {mount_path, revision_number, base, interval_id}
 *
 * Design notes:
 *   - The session id lives in `sessionStorage`, which is per-tab. That is the
 *     addressing unit an agent needs ("which tab is the user looking at"); a cookie
 *     would identify the browser instead.
 *   - Poll pacing is adaptive: the server returns `poll_interval_ms`, and we push it
 *     onto the Interval with `set_props`. Long-polling is deliberately avoided — it
 *     would hold a request open against the app's single-threaded worker server.
 *   - Everything returned is passed through a bounded serializer. Every truncation is
 *     explicit: silent coercion (`undefined` becoming `null`, a 2M-row frame becoming
 *     a plausible-looking short array) would make an agent draw confidently wrong
 *     conclusions.
 *   - Sentinel keys and prop-tier names are mirrored in
 *     `dash_server/session_channel/contract.py`; a test asserts they still agree.
 */

function (nIntervals, meta) {
  "use strict";

  if (!meta || typeof meta.mount_path !== "string" || typeof meta.base !== "string") {
    return "";
  }

  var W = window;
  var STATE_KEY = "__dashServerSessionChannel";
  if (!W[STATE_KEY]) {
    W[STATE_KEY] = install(meta);
  }
  W[STATE_KEY].tick();
  return "";

  // -------------------------------------------------------------------------
  // Install
  // -------------------------------------------------------------------------

  function install(meta) {
    var SENTINEL_TYPE = "$dsType";
    var SENTINEL_LENGTH = "$dsLength";
    var SENTINEL_ITEMS = "$dsItems";
    var SENTINEL_TRUNCATED = "$dsTruncated";
    var SENTINEL_OMITTED_ITEMS = "$dsOmittedItems";
    var SENTINEL_OMITTED_KEYS = "$dsOmittedKeys";
    var SENTINEL_OMITTED_CHARS = "$dsOmittedChars";

    var TIER_DASH_COMPONENT_API = "dash_component_api";
    var TIER_REACT_FIBER = "react_fiber";
    var TIER_DOM = "dom";
    var TIER_NONE = "none";

    var MAX_DEPTH = 6;
    var MAX_ITEMS = 200;
    var MAX_KEYS = 200;
    var MAX_STRING = 4000;
    var MAX_NODES = 5000;
    var IDLE_QUIET_MS = 150;

    var state = {
      base: meta.base.replace(/\/$/, ""),
      mountPath: meta.mount_path,
      revisionNumber: typeof meta.revision_number === "number" ? meta.revision_number : null,
      intervalId: typeof meta.interval_id === "string" ? meta.interval_id : null,
      sessionId: readSessionId(),
      registered: false,
      registering: false,
      polling: false,
      running: false,
      pollIntervalMs: null,
      lastTier: null,
      prologueOffset: measurePrologueOffset()
    };

    var tracker = installFetchTracker();

    return {tick: tick, state: state};

    // ---- lifecycle --------------------------------------------------------

    function tick() {
      if (state.running) {
        return; // A command is executing; do not stack polls behind it.
      }
      if (!state.registered) {
        register();
        return;
      }
      poll();
    }

    function readSessionId() {
      var key = "__dash_server_session_id";
      var existing = null;
      try {
        existing = W.sessionStorage.getItem(key);
      } catch (err) {
        existing = null; // Private mode / blocked storage: fall back to in-memory.
      }
      if (existing) {
        return existing;
      }
      var generated = randomId();
      try {
        W.sessionStorage.setItem(key, generated);
      } catch (err) {
        /* in-memory only for this page load */
      }
      return generated;
    }

    function randomId() {
      try {
        if (W.crypto && typeof W.crypto.randomUUID === "function") {
          return W.crypto.randomUUID().replace(/-/g, "");
        }
      } catch (err) {
        /* fall through */
      }
      return String(Date.now()) + Math.random().toString(16).slice(2, 10);
    }

    function register() {
      if (state.registering) {
        return;
      }
      state.registering = true;
      postJson(state.base + "/register", {
        session_id: state.sessionId,
        mount_path: state.mountPath,
        revision_number: state.revisionNumber,
        pathname: W.location.pathname,
        capabilities: probeCapabilities()
      })
        .then(function (payload) {
          state.registered = true;
          state.registering = false;
          applyPollInterval(payload && payload.poll_interval_ms);
        })
        .catch(function () {
          // Channel disabled or control plane unreachable: stay unregistered and
          // retry on the next tick. No console noise — a hosted-mode page never
          // gets this far, and a local operator restarting the server is normal.
          state.registering = false;
        });
    }

    function poll() {
      if (state.polling) {
        return;
      }
      state.polling = true;
      var url =
        state.base +
        "/poll?session_id=" +
        encodeURIComponent(state.sessionId) +
        "&pathname=" +
        encodeURIComponent(W.location.pathname);
      getJson(url)
        .then(function (payload) {
          state.polling = false;
          if (!payload) {
            return;
          }
          applyPollInterval(payload.poll_interval_ms);
          if (payload.register_required) {
            state.registered = false;
            return;
          }
          if (payload.command) {
            execute(payload.command);
          }
        })
        .catch(function () {
          state.polling = false;
          state.registered = false;
        });
    }

    function applyPollInterval(intervalMs) {
      if (typeof intervalMs !== "number" || intervalMs <= 0) {
        return;
      }
      if (state.pollIntervalMs === intervalMs) {
        return;
      }
      state.pollIntervalMs = intervalMs;
      if (!state.intervalId) {
        return;
      }
      // `set_props` is the supported clientside write API. If it is missing we keep
      // the Interval's configured pace rather than failing — the channel still works,
      // just at the idle cadence.
      try {
        if (W.dash_clientside && typeof W.dash_clientside.set_props === "function") {
          W.dash_clientside.set_props(state.intervalId, {interval: intervalMs});
        }
      } catch (err) {
        /* pacing is best-effort */
      }
    }

    // ---- command execution ------------------------------------------------

    function execute(command) {
      state.running = true;
      var started = now();
      var budget = {truncated: false, nodes: 0};
      var consoleEntries = [];
      var restoreConsole = captureConsole(consoleEntries, budget);
      state.lastTier = null;

      var compiled;
      try {
        compiled = compileCode(command.code);
      } catch (err) {
        restoreConsole();
        finish(command, {
          ok: false,
          // Compile failure: pass a null offset so no misleading line is reported.
          error: describeError(err, null),
          console: consoleEntries,
          duration_ms: now() - started,
          eval_mode: null
        });
        return;
      }

      var ctx = buildContext(budget);
      var timeoutMs = Math.max(1, (command.timeout_seconds || 10) * 1000);
      var settled = false;

      var timer = W.setTimeout(function () {
        if (settled) {
          return;
        }
        settled = true;
        restoreConsole();
        // The page cannot cancel a running promise; report what we have and let the
        // server's own deadline be the authority on the agent-visible outcome.
        finish(command, {
          ok: false,
          error: {name: "TimeoutError", message: "Evaluation exceeded " + command.timeout_seconds + "s in the page."},
          out: serialize(ctx.out, budget, 0, []),
          console: consoleEntries,
          truncated: budget.truncated,
          duration_ms: now() - started,
          eval_mode: compiled.mode,
          tier_used: state.lastTier
        });
      }, timeoutMs);

      var running;
      try {
        running = Promise.resolve(compiled.fn(ctx));
      } catch (err) {
        running = Promise.reject(err);
      }

      running.then(
        function (value) {
          if (settled) {
            return;
          }
          settled = true;
          W.clearTimeout(timer);
          restoreConsole();
          finish(command, {
            ok: true,
            value: serialize(value, budget, 0, []),
            out: serialize(ctx.out, budget, 0, []),
            console: consoleEntries,
            truncated: budget.truncated,
            duration_ms: now() - started,
            eval_mode: compiled.mode,
            tier_used: state.lastTier
          });
        },
        function (err) {
          if (settled) {
            return;
          }
          settled = true;
          W.clearTimeout(timer);
          restoreConsole();
          finish(command, {
            ok: false,
            error: describeError(err, compiled.lineOffset),
            out: serialize(ctx.out, budget, 0, []),
            console: consoleEntries,
            truncated: budget.truncated,
            duration_ms: now() - started,
            eval_mode: compiled.mode,
            tier_used: state.lastTier
          });
        }
      );
    }

    function finish(command, payload) {
      payload.session_id = state.sessionId;
      payload.command_id = command.command_id;
      postJson(state.base + "/result", payload)
        .catch(function () {
          /* the server's deadline covers a lost result */
        })
        .then(function () {
          state.running = false;
          // Poll again promptly: the agent is likely mid-conversation and the server
          // is still returning the active interval.
          poll();
        });
    }

    /*
     * Compile the submitted code so a trailing expression becomes the result.
     *
     * Three attempts, and the mode used is reported back so an agent can tell why a
     * value did or did not come back:
     *   expression - the whole body is one expression (`ctx.props(['a'])`)
     *   last_line  - multi-statement body whose final line is a standalone expression
     *   statements - plain body; use an explicit `return` to produce a value
     *
     * `lineOffset` records how many wrapper lines precede the user's first line, so a
     * thrown error can be reported at a line number relative to what was submitted.
     */
    function compileCode(code) {
      try {
        return {
          fn: new Function("ctx", '"use strict"; return (async function(){\nreturn (\n' + code + "\n);\n})();"),
          mode: "expression",
          lineOffset: 2
        };
      } catch (err) {
        /* not a single expression */
      }
      var rewritten = returnLastLine(code);
      if (rewritten !== null) {
        try {
          return {
            fn: new Function("ctx", '"use strict"; return (async function(){\n' + rewritten + "\n})();"),
            mode: "last_line",
            lineOffset: 1
          };
        } catch (err) {
          /* last line was not an expression after all */
        }
      }
      return {
        fn: new Function("ctx", '"use strict"; return (async function(){\n' + code + "\n})();"),
        mode: "statements",
        lineOffset: 1
      };
    }

    function returnLastLine(code) {
      var lines = code.split("\n");
      var index = -1;
      for (var i = lines.length - 1; i >= 0; i--) {
        if (lines[i].trim() !== "") {
          index = i;
          break;
        }
      }
      if (index < 0) {
        return null;
      }
      var trimmed = lines[index].trim();
      // Word-boundary matching matters here: a plain `indexOf` prefix test would
      // disqualify `format(x)` because it starts with `for`, and `letters` because it
      // starts with `let`.
      if (/^(return|if|for|while|switch|try|catch|finally|else|do|const|let|var|function|class|throw)\b/.test(trimmed)) {
        return null;
      }
      var first = trimmed.charAt(0);
      if (first === "}" || first === "{" || first === "*") {
        return null;
      }
      if (trimmed.indexOf("//") === 0 || trimmed.indexOf("/*") === 0) {
        return null;
      }
      var expression = trimmed.replace(/;$/, "");
      var copy = lines.slice();
      copy[index] = "return (" + expression + ");";
      return copy.join("\n");
    }

    function describeError(err, lineOffset) {
      var described = {
        name: err && err.name ? String(err.name) : "Error",
        message: err && err.message ? String(err.message) : String(err)
      };
      if (err && err.stack) {
        described.stack = String(err.stack).split("\n").slice(0, 12).join("\n");
        // `lineOffset === null` means the code never compiled, so no stack line maps
        // onto anything the agent wrote.
        if (lineOffset !== null && lineOffset !== undefined) {
          var line = extractLine(described.stack, lineOffset);
          if (line !== null) {
            described.line = line;
          }
        }
      }
      return described;
    }

    /*
     * Measure how many lines `new Function` puts in front of a body, on this engine.
     *
     * `new Function(body)` compiles as `function anonymous(...)\n) {\n<body>`, so a stack
     * line is offset from the body by an engine-specific amount (2 on V8). Hardcoding
     * that would silently misreport line numbers on a different engine, so we compile a
     * probe that throws on body line 1 and read back whatever line the engine claims.
     */
    function measurePrologueOffset() {
      try {
        new Function("throw new Error('dash-server line probe');")();
      } catch (err) {
        if (err && err.stack) {
          var reported = rawStackLine(String(err.stack));
          if (reported !== null && reported >= 1) {
            return reported - 1;
          }
        }
      }
      return 2;
    }

    function rawStackLine(stack) {
      var match = /<anonymous>:(\d+):(\d+)/.exec(stack);
      if (!match) {
        match = /:(\d+):(\d+)/.exec(stack);
      }
      if (!match) {
        return null;
      }
      var value = parseInt(match[1], 10);
      return isNaN(value) ? null : value;
    }

    /*
     * Map a stack line number back onto the submitted code.
     *
     * Two shifts stack up: the `new Function` prologue (measured above) and the wrapper
     * this payload adds around the body (`lineOffset`, which varies by compile mode).
     * Reporting a raw stack number would point the agent at a line it never wrote, so a
     * value that lands outside the submitted range is dropped rather than guessed at.
     */
    function extractLine(stack, lineOffset) {
      var reported = rawStackLine(stack);
      if (reported === null) {
        return null;
      }
      var line = reported - (lineOffset || 0) - state.prologueOffset;
      if (isNaN(line) || line < 1) {
        return null;
      }
      return line;
    }

    function captureConsole(entries, budget) {
      var levels = ["log", "info", "warn", "error", "debug"];
      var original = {};
      levels.forEach(function (level) {
        if (typeof W.console[level] !== "function") {
          return;
        }
        original[level] = W.console[level];
        W.console[level] = function () {
          try {
            if (entries.length < 200) {
              entries.push({level: level, text: formatConsoleArgs(arguments, budget)});
            } else {
              budget.truncated = true;
            }
          } catch (err) {
            /* never let capture break the page */
          }
          return original[level].apply(W.console, arguments);
        };
      });
      return function restore() {
        levels.forEach(function (level) {
          if (original[level]) {
            W.console[level] = original[level];
          }
        });
      };
    }

    function formatConsoleArgs(args, budget) {
      var parts = [];
      for (var i = 0; i < args.length; i++) {
        var arg = args[i];
        if (typeof arg === "string") {
          parts.push(arg);
        } else {
          try {
            parts.push(JSON.stringify(serialize(arg, budget, MAX_DEPTH - 2, [])));
          } catch (err) {
            parts.push(String(arg));
          }
        }
      }
      return clipText(parts.join(" "), MAX_STRING, budget);
    }

    // ---- the ctx helper library -------------------------------------------

    function buildContext(budget) {
      var ctx = {
        out: {},
        dash: W.dash_clientside,
        byId: function (id) {
          return document.getElementById(id);
        },
        summarize: function (value, opts) {
          // `opts.depth` is "levels I want"; the serializer counts downward from a
          // starting depth, so convert and clamp rather than allowing a caller to
          // exceed MAX_DEPTH with a large value.
          var levels = opts && typeof opts.depth === "number" ? opts.depth : MAX_DEPTH;
          var start = Math.max(0, MAX_DEPTH - Math.min(levels, MAX_DEPTH));
          return serialize(value, budget, start, []);
        },
        props: function (ids) {
          return readProps(ids, budget);
        },
        dom: function (ids) {
          return readDom(ids);
        },
        plots: function () {
          return readPlots(budget);
        },
        stores: function () {
          return readStores(budget);
        },
        page: function () {
          return readPage();
        },
        setProps: function (id, props) {
          return setProps(id, props);
        },
        waitForIdle: function (ms) {
          return waitForIdle(typeof ms === "number" ? ms : 3000);
        },
        session: {
          session_id: state.sessionId,
          mount_path: state.mountPath,
          revision_number: state.revisionNumber
        }
      };
      return ctx;
    }

    function probeCapabilities() {
      return {
        prop_tier: detectTier(),
        dash_component_api: hasComponentApi(),
        react_fiber: hasReactFiber(),
        set_props: !!(W.dash_clientside && typeof W.dash_clientside.set_props === "function"),
        plotly: document.querySelectorAll(".js-plotly-plot").length > 0,
        session_storage: hasSessionStorage()
      };
    }

    function hasSessionStorage() {
      try {
        W.sessionStorage.getItem("__dash_server_probe");
        return true;
      } catch (err) {
        return false;
      }
    }

    function hasComponentApi() {
      try {
        return !!(W.dash_component_api && typeof W.dash_component_api.getLayout === "function");
      } catch (err) {
        return false;
      }
    }

    function hasReactFiber() {
      var nodes = document.querySelectorAll("[id]");
      for (var i = 0; i < nodes.length && i < 50; i++) {
        if (fiberFor(nodes[i])) {
          return true;
        }
      }
      return false;
    }

    function detectTier() {
      if (hasComponentApi()) {
        return TIER_DASH_COMPONENT_API;
      }
      if (hasReactFiber()) {
        return TIER_REACT_FIBER;
      }
      if (document.querySelectorAll("[id]").length > 0) {
        return TIER_DOM;
      }
      return TIER_NONE;
    }

    function fiberFor(node) {
      for (var key in node) {
        if (key.indexOf("__reactFiber$") === 0) {
          return node[key];
        }
      }
      return null;
    }

    /*
     * Read component props, reporting which mechanism actually worked.
     *
     * Tier 1 (`dash_component_api`) is a supported surface; tier 2 (React fiber
     * traversal) is not and is version-fragile — the project pins dash>=4.3,<5.0, so
     * renderer internals are a moving target. Tier 3 (DOM) can only see what is
     * rendered, so it is flagged `partial`. Degradation is always reported: silently
     * returning a partial prop set as if it were complete is worse than returning
     * nothing.
     */
    function readProps(ids, budget) {
      var requested = normalizeIds(ids);
      var tier = detectTier();
      state.lastTier = tier;
      var values = {};
      var missing = [];
      var partial = false;

      if (tier === TIER_DASH_COMPONENT_API) {
        var index = layoutIndex();
        requested = requested.length ? requested : Object.keys(index);
        requested.forEach(function (id) {
          var props = index[id];
          if (!props) {
            missing.push(id);
            return;
          }
          collectProps(values, id, props, budget);
        });
      } else if (tier === TIER_REACT_FIBER) {
        requested = requested.length ? requested : domIds();
        requested.forEach(function (id) {
          var props = fiberPropsForId(id);
          if (!props) {
            missing.push(id);
            return;
          }
          collectProps(values, id, props, budget);
        });
      } else {
        partial = true;
        requested = requested.length ? requested : domIds();
        requested.forEach(function (id) {
          var node = document.getElementById(id);
          if (!node) {
            missing.push(id);
            return;
          }
          if ("value" in node) {
            values[id + ".value"] = serialize(node.value, budget, 0, []);
          }
          values[id + ".textContent"] = serialize(clip(node.textContent || "", 500, budget), budget, 0, []);
        });
      }

      return {tier: tier, partial: partial, values: values, missing: missing};
    }

    function collectProps(values, id, props, budget) {
      Object.keys(props).forEach(function (prop) {
        if (prop === "children" || prop === "setProps" || prop === "loading_state") {
          return;
        }
        values[id + "." + prop] = serialize(props[prop], budget, 1, []);
      });
    }

    function normalizeIds(ids) {
      if (typeof ids === "string") {
        return [ids];
      }
      if (Object.prototype.toString.call(ids) === "[object Array]") {
        return ids.filter(function (id) {
          return typeof id === "string";
        });
      }
      return [];
    }

    function domIds() {
      var found = [];
      var nodes = document.querySelectorAll("[id]");
      for (var i = 0; i < nodes.length && found.length < MAX_KEYS; i++) {
        var id = nodes[i].getAttribute("id");
        if (id && id.indexOf("__dash-server") !== 0 && id.indexOf("_dash-") !== 0) {
          found.push(id);
        }
      }
      return found;
    }

    function layoutIndex() {
      var index = {};
      try {
        walkLayout(W.dash_component_api.getLayout(), index, 0);
      } catch (err) {
        /* probe said it was available; treat a failure as an empty index */
      }
      return index;
    }

    function walkLayout(node, index, depth) {
      if (!node || depth > 40) {
        return;
      }
      if (Object.prototype.toString.call(node) === "[object Array]") {
        for (var i = 0; i < node.length; i++) {
          walkLayout(node[i], index, depth + 1);
        }
        return;
      }
      if (typeof node !== "object") {
        return;
      }
      var props = node.props || null;
      if (props && typeof props.id === "string") {
        index[props.id] = props;
      }
      if (props) {
        walkLayout(props.children, index, depth + 1);
        // Components can hold layout in props other than `children` (Tabs, Loading,
        // and friends), so scan object/array props one level for nested components.
        Object.keys(props).forEach(function (key) {
          if (key === "children") {
            return;
          }
          var candidate = props[key];
          if (candidate && typeof candidate === "object") {
            walkLayout(candidate, index, depth + 1);
          }
        });
      }
    }

    function fiberPropsForId(id) {
      var node = document.getElementById(id);
      if (!node) {
        return null;
      }
      var fiber = fiberFor(node);
      var hops = 0;
      while (fiber && hops < 30) {
        var props = fiber.memoizedProps;
        if (props && props.id === id) {
          return props;
        }
        fiber = fiber.return;
        hops++;
      }
      // No ancestor fiber carried the Dash id: fall back to the DOM element's own
      // React props, which is still better than nothing for plain html.* components.
      for (var key in node) {
        if (key.indexOf("__reactProps$") === 0) {
          return node[key];
        }
      }
      return null;
    }

    function readDom(ids) {
      var requested = normalizeIds(ids);
      var targets = requested.length ? requested : domIds();
      var described = {};
      var missing = [];
      targets.forEach(function (id) {
        var node = document.getElementById(id);
        if (!node) {
          missing.push(id);
          return;
        }
        described[id] = describeNode(node);
      });
      return {nodes: described, missing: missing, viewport: viewport()};
    }

    function describeNode(node) {
      var rect = null;
      try {
        var box = node.getBoundingClientRect();
        rect = {
          x: Math.round(box.left),
          y: Math.round(box.top),
          width: Math.round(box.width),
          height: Math.round(box.height)
        };
      } catch (err) {
        rect = null;
      }
      var visible = false;
      try {
        if (typeof node.checkVisibility === "function") {
          visible = node.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
        } else {
          var style = W.getComputedStyle(node);
          visible =
            style.display !== "none" &&
            style.visibility !== "hidden" &&
            parseFloat(style.opacity || "1") > 0 &&
            !!rect &&
            rect.width > 0 &&
            rect.height > 0;
        }
      } catch (err) {
        visible = false;
      }
      var inViewport = false;
      if (rect) {
        inViewport =
          rect.y + rect.height > 0 &&
          rect.y < (W.innerHeight || 0) &&
          rect.x + rect.width > 0 &&
          rect.x < (W.innerWidth || 0);
      }
      return {
        tag: String(node.tagName || "").toLowerCase(),
        id: node.getAttribute("id"),
        classes: String(node.className || "").split(/\s+/).filter(Boolean).slice(0, 20),
        text_prefix: String(node.textContent || "").trim().slice(0, 200),
        rect: rect,
        visible: !!visible,
        in_viewport: inViewport,
        child_count: node.children ? node.children.length : 0
      };
    }

    function viewport() {
      return {
        width: W.innerWidth || null,
        height: W.innerHeight || null,
        device_pixel_ratio: W.devicePixelRatio || null,
        scroll_x: W.scrollX || 0,
        scroll_y: W.scrollY || 0
      };
    }

    /*
     * Plotly figure state, read off the graph div.
     *
     * This is the one place where client-side-only state is visible: zoom, pan, and
     * box/lasso selections live in `_fullLayout` and never reach the server, so a
     * server-side journal cannot see them at all.
     */
    function readPlots(budget) {
      var plots = [];
      var nodes = document.querySelectorAll(".js-plotly-plot");
      for (var i = 0; i < nodes.length && i < 25; i++) {
        var gd = nodes[i];
        var data = gd.data || [];
        var full = gd._fullLayout || {};
        var layout = gd.layout || {};
        var traceTypes = [];
        var pointsPerTrace = [];
        var selectedPoints = [];
        for (var t = 0; t < data.length && t < 50; t++) {
          var trace = data[t] || {};
          traceTypes.push(trace.type || "scatter");
          pointsPerTrace.push(traceLength(trace));
          if (trace.selectedpoints) {
            selectedPoints.push({trace: t, count: trace.selectedpoints.length});
          }
        }
        plots.push({
          id: gd.getAttribute ? gd.getAttribute("id") : null,
          trace_count: data.length,
          trace_types: traceTypes,
          points_per_trace: pointsPerTrace,
          layout: {
            "xaxis.range": serialize(axisRange(full, layout, "xaxis"), budget, 2, []),
            "yaxis.range": serialize(axisRange(full, layout, "yaxis"), budget, 2, []),
            title: serialize(layout.title || null, budget, 3, []),
            dragmode: full.dragmode || layout.dragmode || null
          },
          selection: serialize(full.selections || layout.selections || null, budget, 2, []),
          selected_points: selectedPoints
        });
      }
      return plots;
    }

    function axisRange(full, layout, axis) {
      var fromFull = full && full[axis] && full[axis].range;
      if (fromFull) {
        return fromFull;
      }
      return (layout && layout[axis] && layout[axis].range) || null;
    }

    function traceLength(trace) {
      var candidates = ["y", "x", "z", "values", "labels"];
      for (var i = 0; i < candidates.length; i++) {
        var series = trace[candidates[i]];
        if (series && typeof series.length === "number") {
          return series.length;
        }
      }
      return null;
    }

    /*
     * `dcc.Store` contents.
     *
     * A Store renders nothing, so its data is reachable only through the prop tiers;
     * on the DOM tier we can still report persisted stores out of browser storage.
     * `partial` says which of those happened.
     */
    function readStores(budget) {
      var tier = detectTier();
      state.lastTier = tier;
      var stores = {};
      var partial = true;

      if (tier === TIER_DASH_COMPONENT_API) {
        partial = false;
        var index = layoutIndex();
        Object.keys(index).forEach(function (id) {
          var props = index[id];
          if (props && Object.prototype.hasOwnProperty.call(props, "storage_type") &&
              Object.prototype.hasOwnProperty.call(props, "data")) {
            stores[id] = {
              storage_type: props.storage_type,
              data: serialize(props.data, budget, 1, [])
            };
          }
        });
      }

      return {
        tier: tier,
        partial: partial,
        stores: stores,
        local_storage_keys: storageKeys(W.localStorage),
        session_storage_keys: storageKeys(W.sessionStorage)
      };
    }

    function storageKeys(storage) {
      var keys = [];
      try {
        for (var i = 0; i < storage.length && keys.length < 100; i++) {
          keys.push(storage.key(i));
        }
      } catch (err) {
        return [];
      }
      return keys;
    }

    function readPage() {
      var timing = null;
      try {
        var entries = W.performance.getEntriesByType("navigation");
        if (entries && entries.length) {
          var nav = entries[0];
          timing = {
            dom_content_loaded_ms: Math.round(nav.domContentLoadedEventEnd || 0),
            load_event_ms: Math.round(nav.loadEventEnd || 0),
            response_end_ms: Math.round(nav.responseEnd || 0),
            transfer_size: nav.transferSize || null
          };
        }
      } catch (err) {
        timing = null;
      }
      return {
        pathname: W.location.pathname,
        search: W.location.search,
        hash: W.location.hash,
        title: document.title,
        mount_path: state.mountPath,
        revision_number: state.revisionNumber,
        viewport: viewport(),
        navigation_timing: timing
      };
    }

    function setProps(id, props) {
      if (typeof id !== "string" || !props || typeof props !== "object") {
        throw new Error("ctx.setProps(id, props) needs a component id string and a props object.");
      }
      if (!(W.dash_clientside && typeof W.dash_clientside.set_props === "function")) {
        throw new Error("dash_clientside.set_props is unavailable in this renderer.");
      }
      W.dash_clientside.set_props(id, props);
      return {applied: Object.keys(props), id: id};
    }

    /*
     * Resolve once callback traffic has gone quiet.
     *
     * Pairs with `ctx.setProps`: set a real prop, let Dash react exactly as it would
     * for the user, then report which outputs fired. The tracker patches `fetch` at
     * install time to watch `_dash-update-component` traffic.
     */
    function waitForIdle(maxMs) {
      var started = now();
      tracker.fired = [];
      tracker.lastSettle = 0;
      return new Promise(function (resolve) {
        function check() {
          var elapsed = now() - started;
          var quietFor = tracker.lastSettle ? now() - tracker.lastSettle : 0;
          var idle = tracker.inflight === 0 && tracker.lastSettle !== 0 && quietFor >= IDLE_QUIET_MS;
          if (idle || elapsed >= maxMs) {
            resolve({
              fired: dedupe(tracker.fired),
              idle_after_ms: elapsed,
              timed_out: !idle,
              inflight: tracker.inflight
            });
            return;
          }
          W.setTimeout(check, 25);
        }
        check();
      });
    }

    function dedupe(items) {
      var seen = {};
      var result = [];
      items.forEach(function (item) {
        if (!seen[item]) {
          seen[item] = true;
          result.push(item);
        }
      });
      return result;
    }

    function installFetchTracker() {
      var local = {inflight: 0, fired: [], lastSettle: 0};
      if (typeof W.fetch !== "function" || W.__dashServerFetchPatched) {
        return W.__dashServerFetchTracker || local;
      }
      var original = W.fetch;
      W.__dashServerFetchPatched = true;
      W.__dashServerFetchTracker = local;
      W.fetch = function (input, init) {
        var url = typeof input === "string" ? input : (input && input.url) || "";
        var isCallback = url.indexOf("_dash-update-component") !== -1;
        if (!isCallback) {
          return original.apply(this, arguments);
        }
        local.inflight++;
        return original.apply(this, arguments).then(
          function (response) {
            local.inflight = Math.max(0, local.inflight - 1);
            local.lastSettle = now();
            try {
              response
                .clone()
                .json()
                .then(function (body) {
                  collectFired(body, local);
                })
                .catch(function () {});
            } catch (err) {
              /* body already consumed elsewhere; idle detection is unaffected */
            }
            return response;
          },
          function (err) {
            local.inflight = Math.max(0, local.inflight - 1);
            local.lastSettle = now();
            throw err;
          }
        );
      };
      return local;
    }

    function collectFired(body, local) {
      if (!body || typeof body !== "object") {
        return;
      }
      var response = body.response;
      if (!response || typeof response !== "object") {
        return;
      }
      Object.keys(response).forEach(function (outputId) {
        var props = response[outputId];
        if (props && typeof props === "object") {
          Object.keys(props).forEach(function (prop) {
            local.fired.push(outputId + "." + prop);
          });
        } else {
          local.fired.push(outputId);
        }
      });
    }

    // ---- bounded serializer ----------------------------------------------

    /*
     * Convert an arbitrary JS value into something JSON-safe and bounded.
     *
     * Values JSON cannot represent become tagged sentinel objects rather than being
     * coerced: `undefined` must not arrive as `null`, and a clipped array must not
     * look like a complete short one. Every cap that fires sets `budget.truncated`,
     * which the server surfaces on the result envelope.
     */
    function serialize(value, budget, depth, seen) {
      budget.nodes++;
      if (budget.nodes > MAX_NODES) {
        budget.truncated = true;
        return sentinel("node-limit");
      }
      if (value === null) {
        return null;
      }
      var type = typeof value;
      if (type === "undefined") {
        return sentinel("undefined");
      }
      if (type === "boolean") {
        return value;
      }
      if (type === "number") {
        if (isNaN(value)) {
          return sentinel("NaN");
        }
        if (!isFinite(value)) {
          return sentinel(value > 0 ? "Infinity" : "-Infinity");
        }
        return value;
      }
      if (type === "bigint") {
        return sentinel("bigint", {value: String(value)});
      }
      if (type === "string") {
        return clip(value, MAX_STRING, budget);
      }
      if (type === "function") {
        return "[Function " + (value.name || "anonymous") + "]";
      }
      if (type === "symbol") {
        return sentinel("symbol", {value: String(value)});
      }
      if (depth > MAX_DEPTH) {
        budget.truncated = true;
        return sentinel("depth-limit");
      }
      if (seen.indexOf(value) !== -1) {
        return sentinel("circular");
      }

      if (W.Node && value instanceof W.Node) {
        return value.nodeType === 1 ? describeNode(value) : sentinel("dom-node");
      }
      if (value instanceof Date) {
        return sentinel("date", {value: value.toISOString()});
      }
      if (value instanceof Error) {
        return {name: value.name, message: clip(String(value.message), MAX_STRING, budget)};
      }

      var nextSeen = seen.concat([value]);

      if (Object.prototype.toString.call(value) === "[object Array]") {
        return serializeArray(value, budget, depth, nextSeen);
      }
      if (W.Map && value instanceof W.Map) {
        return sentinel("map", {size: value.size});
      }
      if (W.Set && value instanceof W.Set) {
        return sentinel("set", {size: value.size});
      }

      return serializeObject(value, budget, depth, nextSeen);
    }

    function serializeArray(value, budget, depth, seen) {
      var items = [];
      var limit = Math.min(value.length, MAX_ITEMS);
      for (var i = 0; i < limit; i++) {
        items.push(serialize(value[i], budget, depth + 1, seen));
      }
      if (value.length <= MAX_ITEMS) {
        return items;
      }
      budget.truncated = true;
      var wrapped = {};
      wrapped[SENTINEL_TYPE] = "array";
      wrapped[SENTINEL_LENGTH] = value.length;
      wrapped[SENTINEL_ITEMS] = items;
      wrapped[SENTINEL_OMITTED_ITEMS] = value.length - MAX_ITEMS;
      wrapped[SENTINEL_TRUNCATED] = true;
      return wrapped;
    }

    function serializeObject(value, budget, depth, seen) {
      var keys;
      try {
        keys = Object.keys(value);
      } catch (err) {
        return sentinel("unreadable-object");
      }
      var result = {};
      var limit = Math.min(keys.length, MAX_KEYS);
      for (var i = 0; i < limit; i++) {
        var key = keys[i];
        try {
          result[key] = serialize(value[key], budget, depth + 1, seen);
        } catch (err) {
          // A getter that throws must not sink the whole snapshot.
          result[key] = sentinel("getter-threw");
        }
      }
      if (keys.length > MAX_KEYS) {
        budget.truncated = true;
        result[SENTINEL_OMITTED_KEYS] = keys.length - MAX_KEYS;
        result[SENTINEL_TRUNCATED] = true;
      }
      return result;
    }

    function sentinel(kind, extra) {
      var payload = {};
      payload[SENTINEL_TYPE] = kind;
      if (extra) {
        Object.keys(extra).forEach(function (key) {
          payload[key] = extra[key];
        });
      }
      return payload;
    }

    /* Always-a-string clip, for fields the wire contract types as text (console). */
    function clipText(text, limit, budget) {
      if (typeof text !== "string" || text.length <= limit) {
        return typeof text === "string" ? text : String(text);
      }
      budget.truncated = true;
      return text.slice(0, limit) + " […" + (text.length - limit) + " chars omitted]";
    }

    function clip(text, limit, budget) {
      if (typeof text !== "string" || text.length <= limit) {
        return text;
      }
      budget.truncated = true;
      var wrapped = {};
      wrapped[SENTINEL_TYPE] = "string";
      wrapped[SENTINEL_ITEMS] = text.slice(0, limit);
      wrapped[SENTINEL_OMITTED_CHARS] = text.length - limit;
      wrapped[SENTINEL_TRUNCATED] = true;
      return wrapped;
    }

    // ---- transport --------------------------------------------------------

    function postJson(url, body) {
      return W.fetch(url, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)
      }).then(readJsonResponse);
    }

    function getJson(url) {
      return W.fetch(url, {credentials: "same-origin", cache: "no-store"}).then(readJsonResponse);
    }

    function readJsonResponse(response) {
      if (!response.ok) {
        throw new Error("session channel HTTP " + response.status);
      }
      return response.json();
    }

    function now() {
      return Date.now();
    }
  }
}
