/*
Usage (Chrome/Edge DevTools):
1) Open page in browser.
2) Open DevTools Console.
3) Paste full script and run.
4) Start guided flow:
   __bookingCapture.lemana()
5) On each step do Ctrl+Shift+Click target.
6) Confirm captured selector:
   __bookingCapture.ok()
   If wrong:
   __bookingCapture.retry()
   If need go one step back:
   __bookingCapture.back()
7) Print env block:
   __bookingCapture.env()
8) Stop:
   __bookingCapture.stop()
*/

(() => {
  const IGNORE_CLASS_PREFIXES = ["ant-", "css-", "sc-", "rc-", "react-", "__"];
  const MAX_TEXT_LENGTH = 100;
  const STATE_PREFIX = "__bookingCaptureState:";

  const LEMANA_FLOW = [
    {
      label: "LOGIN_SSO_BUTTON_SELECTOR",
      hint: "Login page: click SSO entry button.",
    },
    {
      label: "LOGIN_SUBMIT_SELECTOR",
      hint: "Identity provider page: click submit/login button.",
    },
    {
      label: "OTP_CODE_INPUT_SELECTOR",
      hint: "OTP page: click one-time code input (or first OTP digit input).",
    },
    {
      label: "OFFICE_CHOOSE_SELECTOR",
      hint: "/offices page: click choose button for required office.",
    },
    {
      label: "BOOKING_PARAMS_OPEN_SELECTOR",
      hint: "Map page: click booking parameters panel opener.",
    },
    {
      label: "BOOKING_DATE_INPUT_SELECTOR",
      hint: "Click booking date input field.",
    },
    {
      label: "BOOKING_TYPE_SELECTOR",
      hint: "Click booking type dropdown/input.",
    },
    {
      label: "BOOKING_TYPE_OPTION_SELECTOR",
      hint: "Click option inside booking type dropdown (workplace).",
    },
    {
      label: "BOOKING_TIME_FROM_SELECTOR",
      hint: "Click start time input (for example 09:00).",
    },
    {
      label: "BOOKING_TIME_TO_SELECTOR",
      hint: "Click end time input (for example 18:00).",
    },
    {
      label: "BOOKING_PARAMS_CLOSE_OR_APPLY_SELECTOR",
      hint: "Click close/apply button of booking parameters panel.",
    },
    {
      label: "SEAT_SELECTOR_TEMPLATE",
      hint: "Click seat on map (for example seat 17).",
    },
    {
      label: "BOOK_BUTTON_SELECTOR",
      hint: "Click final booking button in seat modal.",
    },
    {
      label: "SUCCESS_SELECTOR",
      hint: "Click success title/text in success modal.",
    },
    {
      label: "SUCCESS_CLOSE_SELECTOR",
      hint: "Click close button on success modal.",
    },
  ];

  function escapeCss(value) {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(value);
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function normText(text) {
    return (text || "").replace(/\s+/g, " ").trim();
  }

  function textSnippet(el) {
    return normText(el.innerText || el.textContent || "").slice(0, MAX_TEXT_LENGTH);
  }

  function stableClasses(el) {
    return Array.from(el.classList || []).filter(
      (cls) => !IGNORE_CLASS_PREFIXES.some((prefix) => cls.startsWith(prefix))
    );
  }

  function attrSelector(el) {
    const dataTestId = el.getAttribute("data-testid");
    if (dataTestId) return `[data-testid="${escapeCss(dataTestId)}"]`;

    const dataTestIdLegacy = el.getAttribute("data-test-id");
    if (dataTestIdLegacy) return `[data-test-id="${escapeCss(dataTestIdLegacy)}"]`;

    const id = el.getAttribute("id");
    if (id && !/^(root|app|main)$/i.test(id)) return `#${escapeCss(id)}`;

    const name = el.getAttribute("name");
    if (name) return `[name="${escapeCss(name)}"]`;

    const role = el.getAttribute("role");
    if (role) return `[role="${escapeCss(role)}"]`;

    const placeholder = el.getAttribute("placeholder");
    if (placeholder) return `[placeholder="${escapeCss(placeholder)}"]`;

    const title = el.getAttribute("title");
    if (title) return `[title="${escapeCss(title)}"]`;

    return "";
  }

  function baseSelector(el) {
    const tag = (el.tagName || "").toLowerCase() || "*";
    const attr = attrSelector(el);
    const classes = stableClasses(el)
      .slice(0, 2)
      .map((cls) => `.${escapeCss(cls)}`)
      .join("");
    return `${tag}${attr}${classes}`;
  }

  function uniqueSelector(el) {
    const root = el.ownerDocument || document;
    const base = baseSelector(el);
    try {
      if (base && root.querySelectorAll(base).length === 1) return base;
    } catch (_err) {}

    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 7) {
      let part = baseSelector(node);
      const parent = node.parentElement;
      if (parent && !attrSelector(node)) {
        const sameTagSiblings = Array.from(parent.children).filter(
          (child) => child.tagName === node.tagName
        );
        if (sameTagSiblings.length > 1) {
          const index = sameTagSiblings.indexOf(node) + 1;
          part += `:nth-of-type(${index})`;
        }
      }
      parts.unshift(part);
      const candidate = parts.join(" > ");
      try {
        if (candidate && root.querySelectorAll(candidate).length === 1) return candidate;
      } catch (_err) {}
      node = node.parentElement;
      depth += 1;
    }

    return parts.join(" > ") || base || "*";
  }

  const state = {
    activeLabel: null,
    records: {},
    handler: null,
    flow: null,
    flowIndex: -1,
    prevWindowName: "",
    pendingCapture: null,
  };

  function currentFlowStep() {
    if (!state.flow) return null;
    if (state.flowIndex < 0 || state.flowIndex >= state.flow.length) return null;
    return state.flow[state.flowIndex];
  }

  function serializeState() {
    return {
      prevWindowName: state.prevWindowName || "",
      records: state.records || {},
      flowKind: state.flow ? "lemana" : null,
      flowIndex: state.flow ? state.flowIndex : -1,
      activeLabel: state.activeLabel || null,
      pendingCapture: state.pendingCapture || null,
    };
  }

  function parsePersistedWindowName() {
    const raw = window.name || "";
    if (!raw.startsWith(STATE_PREFIX)) return null;
    const payload = raw.slice(STATE_PREFIX.length);
    try {
      return JSON.parse(decodeURIComponent(payload));
    } catch (_err) {
      return null;
    }
  }

  function savePersistedState() {
    const current = parsePersistedWindowName();
    const prevWindowName =
      state.prevWindowName ||
      (current && typeof current.prevWindowName === "string"
        ? current.prevWindowName
        : "");
    const payload = serializeState();
    payload.prevWindowName = prevWindowName;
    window.name = STATE_PREFIX + encodeURIComponent(JSON.stringify(payload));
  }

  function clearPersistedState() {
    const persisted = parsePersistedWindowName();
    if (persisted && typeof persisted.prevWindowName === "string") {
      window.name = persisted.prevWindowName;
    } else {
      window.name = "";
    }
  }

  function restorePersistedState() {
    const persisted = parsePersistedWindowName();
    if (!persisted) return false;

    state.prevWindowName =
      typeof persisted.prevWindowName === "string" ? persisted.prevWindowName : "";
    state.records = persisted.records && typeof persisted.records === "object"
      ? persisted.records
      : {};
    state.pendingCapture =
      persisted.pendingCapture && typeof persisted.pendingCapture === "object"
        ? persisted.pendingCapture
        : null;

    if (persisted.flowKind === "lemana" && Number.isInteger(persisted.flowIndex)) {
      state.flow = LEMANA_FLOW;
      state.flowIndex = persisted.flowIndex;
      if (state.flowIndex < 0) state.flowIndex = 0;
      if (state.flowIndex >= state.flow.length) state.flowIndex = state.flow.length - 1;
      const step = currentFlowStep();
      state.activeLabel = step ? step.label : null;
      if (step) {
        console.log(
          `[capture] Restored flow step ${state.flowIndex + 1}/${state.flow.length}: ${step.label}`
        );
        console.log(`[capture] ${step.hint}`);
      }
    } else {
      state.flow = null;
      state.flowIndex = -1;
      state.activeLabel = persisted.activeLabel || null;
    }

    if (state.pendingCapture) {
      console.log(
        `[capture] Pending selector for ${state.pendingCapture.label}: ${state.pendingCapture.selector}`
      );
      console.log("[capture] Confirm: __bookingCapture.ok() or retry: __bookingCapture.retry()");
    }
    return true;
  }

  function printStepPrompt() {
    const step = currentFlowStep();
    if (!step) {
      console.log("[capture] No active step.");
      return;
    }
    console.log(
      `[capture] Step ${state.flowIndex + 1}/${state.flow.length}: ${step.label}`
    );
    console.log(`[capture] ${step.hint}`);
    console.log("[capture] Do Ctrl+Shift+Click then confirm with __bookingCapture.ok()");
  }

  function startFlow(flow) {
    if (!state.prevWindowName && !parsePersistedWindowName()) {
      state.prevWindowName = window.name || "";
    }
    state.flow = flow;
    state.flowIndex = 0;
    state.pendingCapture = null;
    const step = currentFlowStep();
    if (!step) return;
    state.activeLabel = step.label;
    savePersistedState();
    printStepPrompt();
  }

  function nextFlowStep() {
    if (!state.flow) return;
    state.flowIndex += 1;
    const step = currentFlowStep();
    if (!step) {
      state.flow = null;
      state.flowIndex = -1;
      state.activeLabel = null;
      savePersistedState();
      console.log("[capture] Flow finished. Run __bookingCapture.env()");
      return;
    }
    state.activeLabel = step.label;
    savePersistedState();
    printStepPrompt();
  }

  function selectorOf(label) {
    return state.records[label]?.selector || "";
  }

  function templateSeatSelector(rawSelector) {
    if (!rawSelector) return "";
    return rawSelector.replace(/\b17\b/g, "{seat}");
  }

  const api = {
    next(label) {
      state.flow = null;
      state.flowIndex = -1;
      state.pendingCapture = null;
      state.activeLabel = label;
      savePersistedState();
      console.log(`[capture] Waiting for Ctrl+Shift+Click for "${label}".`);
      console.log("[capture] Confirm with __bookingCapture.ok()");
    },

    lemana() {
      startFlow(LEMANA_FLOW);
    },

    ok() {
      if (!state.pendingCapture) {
        console.log("[capture] Nothing to confirm.");
        return;
      }
      const pending = state.pendingCapture;
      state.records[pending.label] = pending.record;
      state.pendingCapture = null;
      console.log(`[capture] Confirmed ${pending.label}: ${pending.selector}`);

      if (state.flow && state.activeLabel === pending.label) {
        nextFlowStep();
      } else {
        state.activeLabel = null;
        savePersistedState();
      }
    },

    retry() {
      if (!state.pendingCapture) {
        console.log("[capture] No pending capture. Click target first.");
        return;
      }
      const label = state.pendingCapture.label;
      state.pendingCapture = null;
      state.activeLabel = label;
      savePersistedState();
      console.log(`[capture] Retry ${label}. Do Ctrl+Shift+Click again.`);
    },

    back() {
      if (!state.flow) {
        console.log("[capture] Back works only in flow mode.");
        return;
      }
      state.pendingCapture = null;
      state.flowIndex = Math.max(0, state.flowIndex - 1);
      const step = currentFlowStep();
      state.activeLabel = step ? step.label : null;
      savePersistedState();
      printStepPrompt();
    },

    skip() {
      const step = currentFlowStep();
      if (!step) {
        console.log("[capture] No active flow step.");
        return;
      }
      state.pendingCapture = null;
      state.records[step.label] = {
        selector: "",
        text: "",
        tag: "",
        role: "",
        className: "",
        placeholder: "",
        skipped: true,
      };
      console.log(`[capture] Skipped ${step.label}`);
      nextFlowStep();
    },

    set(label, selector) {
      state.pendingCapture = null;
      state.records[label] = {
        selector,
        text: "",
        tag: "",
        role: "",
        className: "",
        placeholder: "",
        manual: true,
      };
      savePersistedState();
      console.log(`[capture] Manually set ${label}: ${selector}`);
    },

    reset() {
      state.activeLabel = null;
      state.records = {};
      state.flow = null;
      state.flowIndex = -1;
      state.pendingCapture = null;
      clearPersistedState();
      console.log("[capture] Reset done. Run __bookingCapture.lemana()");
    },

    show() {
      const step = currentFlowStep();
      if (step) {
        console.log(`[capture] Current step: ${step.label} - ${step.hint}`);
      } else {
        console.log(`[capture] Active label: ${state.activeLabel || "(none)"}`);
      }
      if (state.pendingCapture) {
        console.log(
          `[capture] Pending ${state.pendingCapture.label}: ${state.pendingCapture.selector}`
        );
      }
      return {
        flowIndex: state.flowIndex,
        flowLength: state.flow ? state.flow.length : 0,
        activeLabel: state.activeLabel,
        pending: state.pendingCapture,
      };
    },

    dump() {
      const out = JSON.parse(JSON.stringify(state.records));
      console.log(out);
      console.log(JSON.stringify(out, null, 2));
      return out;
    },

    env() {
      const ssoSelector = selectorOf("LOGIN_SSO_BUTTON_SELECTOR");
      const submitSelector = selectorOf("LOGIN_SUBMIT_SELECTOR");
      const otpSelector = selectorOf("OTP_CODE_INPUT_SELECTOR");
      const seatRaw = selectorOf("SEAT_SELECTOR_TEMPLATE");
      const seatTemplate = templateSeatSelector(seatRaw);
      const lines = [
        `PRE_LOGIN_CLICK_SELECTORS=${ssoSelector}`,
        `LOGIN_SUBMIT_SELECTORS=${
          submitSelector || 'button[type="submit"]|button:has-text("Sign in")'
        }`,
        `OTP_CODE_INPUT_SELECTOR=${otpSelector}`,
        `OFFICE_CHOOSE_SELECTOR=${selectorOf("OFFICE_CHOOSE_SELECTOR")}`,
        `BOOKING_PARAMS_OPEN_SELECTOR=${selectorOf("BOOKING_PARAMS_OPEN_SELECTOR")}`,
        `BOOKING_DATE_INPUT_SELECTOR=${selectorOf("BOOKING_DATE_INPUT_SELECTOR")}`,
        `BOOKING_DATE_OFFSET_DAYS=7`,
        `BOOKING_DATE_FORMAT=%d.%m.%Y`,
        `BOOKING_TYPE_SELECTOR=${selectorOf("BOOKING_TYPE_SELECTOR")}`,
        `BOOKING_TYPE_OPTION_SELECTOR=${selectorOf("BOOKING_TYPE_OPTION_SELECTOR")}`,
        `BOOKING_TIME_FROM_SELECTOR=${selectorOf("BOOKING_TIME_FROM_SELECTOR")}`,
        `BOOKING_TIME_TO_SELECTOR=${selectorOf("BOOKING_TIME_TO_SELECTOR")}`,
        `BOOKING_TIME_FROM=09:00`,
        `BOOKING_TIME_TO=18:00`,
        `BOOKING_PARAMS_CLOSE_SELECTOR=${selectorOf(
          "BOOKING_PARAMS_CLOSE_OR_APPLY_SELECTOR"
        )}`,
        `SEAT_SELECTOR_TEMPLATE=${seatTemplate || seatRaw}`,
        `BOOK_BUTTON_SELECTOR=${selectorOf("BOOK_BUTTON_SELECTOR")}`,
        `SUCCESS_SELECTOR=${selectorOf("SUCCESS_SELECTOR")}`,
        `TARGET_SEAT=17`,
      ];
      const out = lines.join("\n");
      console.log(out);
      return out;
    },

    stop() {
      if (state.handler) document.removeEventListener("click", state.handler, true);
      state.activeLabel = null;
      state.flow = null;
      state.flowIndex = -1;
      state.pendingCapture = null;
      clearPersistedState();
      console.log("[capture] Stopped.");
    },
  };

  state.handler = (event) => {
    if (!state.activeLabel) return;
    if (!(event.ctrlKey && event.shiftKey)) return;

    const el = event.target;
    const selector = uniqueSelector(el);
    const text = textSnippet(el);

    state.pendingCapture = {
      label: state.activeLabel,
      selector,
      record: {
        selector,
        text,
        tag: (el.tagName || "").toLowerCase(),
        role: el.getAttribute("role") || "",
        className: el.className || "",
        placeholder: el.getAttribute("placeholder") || "",
      },
    };
    savePersistedState();

    console.log(`[capture] Captured ${state.activeLabel}: ${selector}`);
    console.log("[capture] Confirm with __bookingCapture.ok()");
    console.log("[capture] Wrong target? Use __bookingCapture.retry()");
  };

  document.addEventListener("click", state.handler, true);
  window.__bookingCapture = api;
  console.log("[capture] Ready.");
  console.log("[capture] Run __bookingCapture.lemana() for guided flow.");
  if (!restorePersistedState()) {
    state.prevWindowName = window.name || "";
  }
  console.log(
    "[capture] If page/domain changed: paste script again, state auto-restores."
  );
})();

