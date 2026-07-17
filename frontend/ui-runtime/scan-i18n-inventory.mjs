import process from "node:process";

import { parse } from "acorn";


const ASSIGNMENT_SINKS = new Set([
  "innerHTML",
  "innerText",
  "placeholder",
  "textContent",
  "title",
]);
const ATTRIBUTE_SINKS = new Set([
  "alt",
  "aria-description",
  "aria-label",
  "placeholder",
  "title",
]);
const CALL_SINK_ARGUMENTS = Object.freeze({
  alert: [0],
  confirm: [0],
  createTextNode: [0],
  renderErrorWithDetails: [0, 1],
  renderMessage: [1],
  setFreeTierStatus: [0],
  showToast: [0],
});
const SET_STATUS_ARGUMENTS_BY_SOURCE = Object.freeze({
  "static/editor.js": [],
  "static/gateway-docs.js": [0],
  "static/usage-analytics.js": [0],
  "static/web-playground.js": [1],
});
const INTL_FORMATTERS = new Set([
  "Collator",
  "DateTimeFormat",
  "DisplayNames",
  "ListFormat",
  "NumberFormat",
  "PluralRules",
  "RelativeTimeFormat",
  "Segmenter",
]);
const LOCALE_IDENTIFIERS = new Set([
  "currentlanguage",
  "currentlocale",
  "language",
  "locale",
  "resolvedlanguage",
]);
const LOCALE_METHODS = new Set([
  "toLocaleDateString",
  "toLocaleLowerCase",
  "toLocaleString",
  "toLocaleTimeString",
  "toLocaleUpperCase",
]);
const LANGUAGE_LITERAL = /^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/;


function asciiLower(value) {
  return value.replace(/[A-Z]/g, (letter) => letter.toLowerCase());
}


function propertyName(node) {
  if (!node || node.type !== "MemberExpression") return null;
  if (!node.computed && node.property.type === "Identifier") return node.property.name;
  if (node.computed && node.property.type === "Literal" && typeof node.property.value === "string") {
    return node.property.value;
  }
  return null;
}


function isTranslationCall(node) {
  if (!node || node.type !== "CallExpression") return false;
  if (propertyName(node.callee) !== "t") return false;
  const owner = node.callee.object;
  if (owner.type === "Identifier") {
    return owner.name === "i18n" || owner.name === "gatewayI18n";
  }
  return (
    owner.type === "MemberExpression"
    && propertyName(owner) === "gatewayI18n"
    && owner.object.type === "Identifier"
    && owner.object.name === "window"
  );
}


function isVisibleLiteral(value) {
  return typeof value === "string" && /[\p{L}\p{N}]/u.test(value);
}


function literalText(node, source) {
  if (node.type === "Literal" && typeof node.value === "string") return node.value;
  if (node.type !== "TemplateLiteral") return null;
  const staticText = node.quasis.map((quasi) => quasi.value.cooked ?? quasi.value.raw).join("");
  return isVisibleLiteral(staticText) ? source.slice(node.start + 1, node.end - 1) : null;
}


function finding(sourceEntry, kind, node, text) {
  return {
    page: sourceEntry.page,
    file: sourceEntry.file,
    kind,
    line: node.loc.start.line,
    column: node.loc.start.column + 1,
    text,
  };
}


function collectMessageLiterals(node, sourceEntry, findings) {
  if (!node || isTranslationCall(node)) return;
  const directText = literalText(node, sourceEntry.source);
  if (isVisibleLiteral(directText)) {
    findings.push(finding(sourceEntry, "js-sink", node, directText));
    return;
  }
  switch (node.type) {
    case "ArrayExpression":
      node.elements.forEach((child) => collectMessageLiterals(child, sourceEntry, findings));
      break;
    case "AwaitExpression":
    case "ChainExpression":
    case "ParenthesizedExpression":
    case "UnaryExpression":
      collectMessageLiterals(node.argument ?? node.expression, sourceEntry, findings);
      break;
    case "BinaryExpression":
    case "LogicalExpression":
      collectMessageLiterals(node.left, sourceEntry, findings);
      collectMessageLiterals(node.right, sourceEntry, findings);
      break;
    case "CallExpression":
    case "NewExpression":
      node.arguments.forEach((child) => collectMessageLiterals(child, sourceEntry, findings));
      break;
    case "ConditionalExpression":
      collectMessageLiterals(node.consequent, sourceEntry, findings);
      collectMessageLiterals(node.alternate, sourceEntry, findings);
      break;
    case "ObjectExpression":
      node.properties.forEach((property) => {
        if (property.type === "Property") {
          collectMessageLiterals(property.value, sourceEntry, findings);
        }
      });
      break;
    case "SequenceExpression":
      node.expressions.forEach((child) => collectMessageLiterals(child, sourceEntry, findings));
      break;
    case "TaggedTemplateExpression":
      collectMessageLiterals(node.quasi, sourceEntry, findings);
      break;
    case "TemplateLiteral":
      node.expressions.forEach((child) => collectMessageLiterals(child, sourceEntry, findings));
      break;
    default:
      break;
  }
}


function callName(node) {
  if (node.type === "Identifier") return node.name;
  return propertyName(node);
}


function isIntlFormatter(node) {
  if (!node || node.type !== "MemberExpression") return false;
  return (
    node.object.type === "Identifier"
    && node.object.name === "Intl"
    && INTL_FORMATTERS.has(propertyName(node))
  );
}


function isLocaleExpression(node) {
  if (!node) return false;
  if (node.type === "Identifier") return LOCALE_IDENTIFIERS.has(node.name.toLowerCase());
  const name = propertyName(node);
  return typeof name === "string" && LOCALE_IDENTIFIERS.has(name.toLowerCase());
}


function isLanguageLiteral(node) {
  return (
    node?.type === "Literal"
    && typeof node.value === "string"
    && LANGUAGE_LITERAL.test(node.value)
  );
}


function inspectNode(node, sourceEntry, findings) {
  if (node.type === "AssignmentExpression" && ASSIGNMENT_SINKS.has(propertyName(node.left))) {
    collectMessageLiterals(node.right, sourceEntry, findings);
  }
  if (node.type === "CallExpression") {
    const name = callName(node.callee);
    if (name === "setAttribute") {
      const attribute = node.arguments[0];
      if (
        attribute?.type === "Literal"
        && typeof attribute.value === "string"
        && ATTRIBUTE_SINKS.has(asciiLower(attribute.value))
      ) {
        collectMessageLiterals(node.arguments[1], sourceEntry, findings);
      }
    }
    const indexes = name === "setStatus"
      ? (SET_STATUS_ARGUMENTS_BY_SOURCE[sourceEntry.file] ?? [1])
      : CALL_SINK_ARGUMENTS[name];
    if (Array.isArray(indexes)) {
      indexes.forEach((index) => {
        const before = findings.length;
        collectMessageLiterals(node.arguments[index], sourceEntry, findings);
        for (let position = before; position < findings.length; position += 1) {
          findings[position].kind = "js-call-sink";
        }
      });
    }
    const localeMethod = propertyName(node.callee);
    if (LOCALE_METHODS.has(localeMethod)) {
      findings.push(finding(sourceEntry, "js-intl", node.callee, `${localeMethod}()`));
    } else if (isIntlFormatter(node.callee)) {
      findings.push(finding(sourceEntry, "js-intl", node.callee, `Intl.${localeMethod}()`));
    }
  }
  if (node.type === "NewExpression" && isIntlFormatter(node.callee)) {
    const name = propertyName(node.callee);
    findings.push(finding(sourceEntry, "js-intl", node.callee, `Intl.${name}()`));
  }
  if (
    node.type === "BinaryExpression"
    && ["==", "===", "!=", "!=="].includes(node.operator)
    && (
      (isLocaleExpression(node.left) && isLanguageLiteral(node.right))
      || (isLanguageLiteral(node.left) && isLocaleExpression(node.right))
    )
  ) {
    findings.push(finding(
      sourceEntry,
      "js-locale-branch",
      node,
      sourceEntry.source.slice(node.start, node.end),
    ));
  }
  if (node.type === "SwitchStatement" && isLocaleExpression(node.discriminant)) {
    node.cases.forEach((switchCase) => {
      if (isLanguageLiteral(switchCase.test)) {
        findings.push(finding(
          sourceEntry,
          "js-locale-branch",
          switchCase.test,
          sourceEntry.source.slice(switchCase.test.start, switchCase.test.end),
        ));
      }
    });
  }
}


function walk(node, sourceEntry, findings) {
  if (!node || typeof node !== "object" || typeof node.type !== "string") return;
  inspectNode(node, sourceEntry, findings);
  for (const [key, value] of Object.entries(node)) {
    if (["end", "loc", "start", "type"].includes(key)) continue;
    if (Array.isArray(value)) {
      value.forEach((child) => walk(child, sourceEntry, findings));
    } else {
      walk(value, sourceEntry, findings);
    }
  }
}


function scanSource(sourceEntry) {
  try {
    const tree = parse(sourceEntry.source, {
      allowAwaitOutsideFunction: true,
      allowHashBang: true,
      ecmaVersion: "latest",
      locations: true,
      sourceType: "script",
    });
    const findings = [];
    walk(tree, sourceEntry, findings);
    return { findings, errors: [] };
  } catch (error) {
    const line = error.loc?.line ?? 1;
    const column = (error.loc?.column ?? 0) + 1;
    return {
      findings: [],
      errors: [`cannot parse inventory source ${sourceEntry.file}:${line}:${column}: ${error.message}`],
    };
  }
}


let input = "";
for await (const chunk of process.stdin) input += chunk;
const payload = JSON.parse(input);
const result = { findings: [], errors: [] };
for (const sourceEntry of payload.sources ?? []) {
  const scanned = scanSource(sourceEntry);
  result.findings.push(...scanned.findings);
  result.errors.push(...scanned.errors);
}
result.findings = [...new Map(result.findings.map((item) => [
  [item.page, item.file, item.kind, item.line, item.column, item.text].join("\u0000"),
  item,
])).values()];
result.findings.sort((left, right) => (
  left.file.localeCompare(right.file)
  || left.line - right.line
  || left.column - right.column
  || left.kind.localeCompare(right.kind)
  || left.text.localeCompare(right.text)
));
process.stdout.write(JSON.stringify(result));
