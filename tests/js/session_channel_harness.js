/*
 * Node driver for the session-channel page payload.
 *
 * The payload is a Dash clientside-callback body that normally runs in a browser, so
 * nothing in the Python suite parses it — a syntax error or a broken serializer would
 * otherwise surface for the first time in a user's browser. This harness stubs just
 * enough of `window`/`document`/`fetch` to drive one real command through the payload
 * (register -> poll -> evaluate -> post result) and prints the posted result envelope as
 * JSON on stdout.
 *
 * Usage: node session_channel_harness.js <path-to-session_channel.js> <code>
 * Consumed by tests/test_session_channel_js.py, which skips when node is unavailable.
 *
 * Optional env var SESSION_CHANNEL_FAKE_LAYOUT: a JSON `{components: ...}` Dash layout
 * tree (the shape a real `store.getState().layout` has). When set, this stubs
 * `window.dash_stores`/`window.dash_component_api.getLayout` so PS26-BUG-002 regression
 * tests can exercise the `dash_component_api` prop tier without a real browser. The fake
 * `getLayout` mirrors the real one's one sharp edge on purpose (throws when called with
 * no argument) so a regression back to the old no-arg call is still caught here.
 */

"use strict";

const fs = require("fs");

const payloadPath = process.argv[2];
const codeToRun = process.argv[3];
const payloadSource = fs.readFileSync(payloadPath, "utf8");

const BASE = "/__dash-server/session";
const SESSION_ID_KEY = "__dash_server_session_id";

let postedResult = null;
let registered = null;
let commandDelivered = false;
const setPropsCalls = [];

function makeStorage() {
  const data = {};
  return {
    getItem(key) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null;
    },
    setItem(key, value) {
      data[key] = String(value);
    },
    key(index) {
      return Object.keys(data)[index];
    },
    get length() {
      return Object.keys(data).length;
    },
  };
}

function jsonResponse(body) {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    clone() {
      return jsonResponse(body);
    },
  };
}

const storage = makeStorage();

const win = {
  location: { pathname: "/apps/demo", search: "?a=1", hash: "" },
  sessionStorage: storage,
  localStorage: makeStorage(),
  innerWidth: 1280,
  innerHeight: 800,
  devicePixelRatio: 2,
  scrollX: 0,
  scrollY: 40,
  setTimeout,
  clearTimeout,
  console,
  crypto,
  Node: function StubNode() {},
  Map,
  Set,
  performance: { getEntriesByType: () => [] },
  dash_clientside: {
    set_props: (id, props) => setPropsCalls.push({ id, props }),
  },
  getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
  fetch: async (url, init) => {
    const method = (init && init.method) || "GET";
    if (url.startsWith(BASE + "/register")) {
      registered = JSON.parse(init.body);
      return jsonResponse({ session_id: registered.session_id, app: "demo", poll_interval_ms: 250 });
    }
    if (url.startsWith(BASE + "/poll")) {
      if (commandDelivered) {
        return jsonResponse({ command: null, register_required: false, poll_interval_ms: 2000 });
      }
      commandDelivered = true;
      return jsonResponse({
        command: { command_id: "cmd-1", code: codeToRun, timeout_seconds: 5, command_seq: 1 },
        register_required: false,
        poll_interval_ms: 250,
      });
    }
    if (url.startsWith(BASE + "/result") && method === "POST") {
      postedResult = JSON.parse(init.body);
      return jsonResponse({ accepted: true });
    }
    throw new Error("unexpected fetch: " + method + " " + url);
  },
};

const fakeLayoutJson = process.env.SESSION_CHANNEL_FAKE_LAYOUT;
if (fakeLayoutJson) {
  const fakeTree = JSON.parse(fakeLayoutJson);

  function findById(node, id) {
    if (!node) {
      return null;
    }
    if (Array.isArray(node)) {
      for (const child of node) {
        const found = findById(child, id);
        if (found) {
          return found;
        }
      }
      return null;
    }
    if (typeof node !== "object") {
      return null;
    }
    const props = node.props || null;
    if (props && props.id === id) {
      return node;
    }
    if (props) {
      return findById(props.children, id);
    }
    return null;
  }

  win.dash_component_api = {
    // Real signature: getLayout(componentPathOrId). Called with no argument (the
    // PS26-BUG-002 bug), the real implementation feeds `undefined` into Ramda's
    // `path()`, which throws — reproduced here so a regression is still caught.
    getLayout(componentPathOrId) {
      if (componentPathOrId === undefined) {
        throw new TypeError("Cannot read properties of undefined (reading 'length')");
      }
      const node = findById(fakeTree.components, componentPathOrId);
      return node ? node.props : undefined;
    },
  };
  win.dash_stores = [
    {
      getState() {
        return { layout: fakeTree };
      },
    },
  ];
}

global.window = win;
global.document = {
  title: "Demo dashboard",
  hidden: false,
  getElementById: () => null,
  querySelectorAll: () => [],
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(predicate, timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (predicate()) {
      return true;
    }
    await sleep(5);
  }
  return false;
}

async function main() {
  // The payload is a function *expression*; wrap it so it is a valid expression here.
  const callback = eval("(" + payloadSource + ")");
  const meta = {
    mount_path: "/apps/demo",
    revision_number: 7,
    base: BASE,
    interval_id: "__dash-server-session-interval",
  };

  // Tick repeatedly, the way the `dcc.Interval` does in a real page. A fixed number of
  // ticks would be wrong: registration completes asynchronously, so the tick that polls
  // is not necessarily the second one.
  let ticks = 0;
  const answered = await waitFor(() => {
    callback(ticks++, meta);
    return postedResult !== null;
  }, 8000);

  if (registered === null) {
    throw new Error("payload never registered");
  }
  if (!answered) {
    throw new Error("payload never posted a result after " + ticks + " ticks");
  }

  process.stdout.write(
    JSON.stringify({
      registered,
      result: postedResult,
      set_props_calls: setPropsCalls,
    }) + "\n"
  );
}

main().catch((err) => {
  process.stderr.write(String((err && err.stack) || err) + "\n");
  process.exit(1);
});
