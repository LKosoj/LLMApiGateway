const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
const ABSOLUTE_URL_SCHEME = /^[a-z][a-z\d+.-]*$/i;
const SVG_TAGS = new Set(["circle", "line", "path", "polyline"]);
const SVG_ATTRIBUTES = new Set(["cx", "cy", "d", "points", "r", "x1", "x2", "y1", "y2"]);

const ICONS = Object.freeze({
  docs: Object.freeze([
    ["path", { d: "M4 19.5A2.5 2.5 0 0 1 6.5 17H20" }],
    ["path", { d: "M4 4.5A2.5 2.5 0 0 1 6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5z" }],
  ]),
  usage: Object.freeze([
    ["path", { d: "M3 3v18h18" }],
    ["path", { d: "M18 17V9" }],
    ["path", { d: "M13 17V5" }],
    ["path", { d: "M8 17v-3" }],
  ]),
  quota: Object.freeze([
    ["circle", { cx: "12", cy: "12", r: "10" }],
    ["path", { d: "M12 6v6l4 2" }],
  ]),
  rules: Object.freeze([
    ["path", { d: "M12 20h9" }],
    ["path", { d: "M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" }],
  ]),
  playground: Object.freeze([
    ["circle", { cx: "12", cy: "12", r: "10" }],
    ["path", { d: "M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10" }],
    ["path", { d: "M2 12h20" }],
  ]),
  "api-keys": Object.freeze([
    ["circle", { cx: "8", cy: "15", r: "4" }],
    ["path", { d: "M10.85 12.15 19 4" }],
    ["path", { d: "M18 5l2 2" }],
    ["path", { d: "M15 8l2 2" }],
  ]),
  rejections: Object.freeze([
    ["path", { d: "M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" }],
    ["line", { x1: "12", x2: "12", y1: "9", y2: "13" }],
    ["line", { x1: "12", x2: "12.01", y1: "17", y2: "17" }],
  ]),
  translator: Object.freeze([
    ["polyline", { points: "16 18 22 12 16 6" }],
    ["polyline", { points: "8 6 2 12 8 18" }],
  ]),
  pricing: Object.freeze([
    ["line", { x1: "12", x2: "12", y1: "1", y2: "23" }],
    ["path", { d: "M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" }],
  ]),
  "free-models": Object.freeze([
    ["polyline", { points: "3 7 12 3 21 7 12 11 3 7" }],
    ["polyline", { points: "3 12 12 16 21 12" }],
    ["polyline", { points: "3 17 12 21 21 17" }],
  ]),
});

function navigationItem(id, href, labelKey, roles) {
  return Object.freeze({
    id,
    href,
    labelKey,
    icon: id,
    roles: Object.freeze([...roles]),
  });
}

export const NAV_ITEMS = Object.freeze([
  navigationItem("docs", "/v1/ui/docs", "common:navigation.docs", ["master", "user"]),
  navigationItem("usage", "/v1/ui/usage-stats", "common:navigation.usage", ["master", "user"]),
  navigationItem("quota", "/v1/ui/quota", "common:navigation.quota", ["master", "user"]),
  navigationItem("rules", "/v1/ui/rules-editor", "common:navigation.rules", ["master"]),
  navigationItem("playground", "/v1/ui/playground", "common:navigation.playground", ["master"]),
  navigationItem("api-keys", "/v1/ui/api-keys", "common:navigation.apiKeys", ["master"]),
  navigationItem("rejections", "/v1/ui/rejections", "common:navigation.rejections", ["master"]),
  navigationItem("translator", "/v1/ui/translator-debug", "common:navigation.translator", ["master"]),
  navigationItem("pricing", "/v1/ui/pricing", "common:navigation.pricing", ["master"]),
  navigationItem("free-models", "/v1/ui/free-models", "common:navigation.freeModels", ["master", "user"]),
]);

export function normalizePathname(value) {
  if (typeof value !== "string" || value.length === 0) return "";
  const schemeEnd = value.indexOf("://");
  const hasAbsoluteScheme = schemeEnd > 0
    && ABSOLUTE_URL_SCHEME.test(value.slice(0, schemeEnd));
  if (!value.startsWith("/") && !hasAbsoluteScheme) return "";
  try {
    const pathname = new URL(value, "gateway:/").pathname;
    return pathname.replace(/\/+$/, "") || "/";
  } catch (_error) {
    return "";
  }
}

export function visibleNavigationItems(role) {
  if (role !== "master" && role !== "user") return [];
  return NAV_ITEMS.filter((item) => item.roles.includes(role));
}

function createIcon(document, iconName) {
  const definition = ICONS[iconName];
  if (!definition) throw new TypeError(`Unknown navigation icon: ${iconName}`);

  const icon = document.createElementNS(SVG_NAMESPACE, "svg");
  icon.setAttribute("aria-hidden", "true");
  icon.setAttribute("class", "nav-button-icon");
  icon.setAttribute("fill", "none");
  icon.setAttribute("height", "16");
  icon.setAttribute("stroke", "currentColor");
  icon.setAttribute("stroke-linecap", "round");
  icon.setAttribute("stroke-linejoin", "round");
  icon.setAttribute("stroke-width", "2");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("width", "16");

  for (const [tagName, attributes] of definition) {
    if (!SVG_TAGS.has(tagName)) throw new TypeError(`Unsafe SVG tag: ${tagName}`);
    const child = document.createElementNS(SVG_NAMESPACE, tagName);
    for (const [name, value] of Object.entries(attributes)) {
      if (!SVG_ATTRIBUTES.has(name)) throw new TypeError(`Unsafe SVG attribute: ${name}`);
      child.setAttribute(name, value);
    }
    icon.appendChild(child);
  }
  return icon;
}

function createLink(document, item, currentPath, translate) {
  const link = document.createElement("a");
  link.classList.add("nav-button");
  link.setAttribute("href", item.href);
  link.setAttribute("data-nav-id", item.id);
  link.appendChild(createIcon(document, item.icon));

  const label = document.createElement("span");
  label.classList.add("nav-button-label");
  const translated = translate(item.labelKey);
  if (typeof translated !== "string") {
    throw new TypeError(`Navigation label must be a string: ${item.labelKey}`);
  }
  label.textContent = translated;
  link.appendChild(label);

  if (normalizePathname(item.href) === currentPath) {
    link.classList.add("active");
    link.setAttribute("aria-current", "page");
  }
  return link;
}

export function measureNavigationOverflow(scroller, links, direction = "ltr") {
  if (!scroller || !Array.isArray(links) || links.length === 0) {
    return { start: false, end: false };
  }
  const viewport = scroller.getBoundingClientRect();
  const first = links[0].getBoundingClientRect();
  const last = links.at(-1).getBoundingClientRect();
  const tolerance = 1;

  if (direction === "rtl") {
    return {
      start: first.right > viewport.right + tolerance,
      end: last.left < viewport.left - tolerance,
    };
  }
  return {
    start: first.left < viewport.left - tolerance,
    end: last.right > viewport.right + tolerance,
  };
}

export function createNavigation(root, options = {}) {
  if (!root?.ownerDocument || typeof root.replaceChildren !== "function") {
    throw new TypeError("A navigation mount element is required");
  }

  let config = {
    direction: "ltr",
    pathname: "/",
    role: "pending",
    translate: null,
    ...options,
  };
  let scroller = null;
  let links = [];
  let resizeObserver = null;
  let mutationObserver = null;

  function ensureCurrentVisible() {
    const currentLink = links.find((link) => link.getAttribute("aria-current") === "page");
    currentLink?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function refreshOverflow() {
    const overflow = measureNavigationOverflow(scroller, links, config.direction);
    root.toggleAttribute("data-overflow-start", overflow.start);
    root.toggleAttribute("data-overflow-end", overflow.end);
    return overflow;
  }

  function disconnectObservers() {
    if (scroller) scroller.removeEventListener("scroll", refreshOverflow);
    resizeObserver?.disconnect();
    mutationObserver?.disconnect();
    resizeObserver = null;
    mutationObserver = null;
  }

  function render() {
    disconnectObservers();
    const items = visibleNavigationItems(config.role);
    root.hidden = items.length === 0;
    root.replaceChildren();
    root.removeAttribute("data-overflow-start");
    root.removeAttribute("data-overflow-end");
    scroller = null;
    links = [];
    if (items.length === 0) return;
    if (typeof config.translate !== "function") {
      throw new TypeError("A navigation translator is required");
    }

    const document = root.ownerDocument;
    scroller = document.createElement("div");
    scroller.classList.add("gateway-nav-scroller");
    const currentPath = normalizePathname(config.pathname);
    links = items.map((item) => createLink(document, item, currentPath, config.translate));
    scroller.append(...links);
    root.replaceChildren(scroller);
    const ariaLabel = config.translate("common:navigation.label");
    if (typeof ariaLabel !== "string") {
      throw new TypeError("Navigation aria-label must be a string");
    }
    root.setAttribute("aria-label", ariaLabel);
    scroller.addEventListener("scroll", refreshOverflow, { passive: true });

    ensureCurrentVisible();
    refreshOverflow();

    const ResizeObserverClass = config.ResizeObserver ?? globalThis.ResizeObserver;
    if (typeof ResizeObserverClass === "function") {
      resizeObserver = new ResizeObserverClass(() => {
        ensureCurrentVisible();
        refreshOverflow();
      });
      resizeObserver.observe(scroller);
    }
    const MutationObserverClass = config.MutationObserver ?? globalThis.MutationObserver;
    if (typeof MutationObserverClass === "function") {
      mutationObserver = new MutationObserverClass(() => {
        ensureCurrentVisible();
        refreshOverflow();
      });
      mutationObserver.observe(scroller, { childList: true, subtree: true, characterData: true });
    }
  }

  const controller = Object.freeze({
    destroy() {
      disconnectObservers();
    },
    refreshOverflow,
    update(next = {}) {
      config = { ...config, ...next };
      render();
      return controller;
    },
  });
  render();
  return controller;
}
