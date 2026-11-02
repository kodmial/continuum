'use strict';

// BEGIN continuum-workflow-dispatch-contract
// Structural parser for the workflow_dispatch contract of a caller workflow.
//
// The controller dispatches a sibling caller only when the exact dispatch ref
// accepts workflow_dispatch and every input the dispatch sends. Rather than
// matching fixed indentation with regular expressions, this parses the YAML
// subset a workflow file uses: block mappings at any indentation, flow maps
// and lists, quoted scalars, comments, CRLF, folded/literal block scalars,
// block sequences (parsed as lists so an `on:` sequence works; their nested
// continuation lines never read as mapping siblings), and the document
// marker rules GitHub applies (exactly one document).
// The embedded copies in the controller workflows are generated from this
// file by scripts/embed_workflow_dispatch_contract.py; tests fail if they
// drift.
function parseWorkflowDispatchContract(text, label, requiredInputs = []) {
  const hasOwn = (value, key) =>
    Object.prototype.hasOwnProperty.call(value, key);

  const fail = (reason, inputs = []) => ({ ok: false, reason, inputs });

  const leadingIndent = value => {
    const match = /^[ \t]*/.exec(value);
    return match ? match[0] : '';
  };

  const stripComment = value => {
    let quote = '';
    let depth = 0;
    for (let index = 0; index < value.length; index += 1) {
      const character = value[index];
      if (quote) {
        if (quote === '"' && character === '\\') {
          index += 1;
        } else if (quote === "'" && character === "'" &&
                   value[index + 1] === "'") {
          index += 1;
        } else if (character === quote) {
          quote = '';
        }
        continue;
      }
      if (character === '"' || character === "'") {
        quote = character;
      } else if (character === '{' || character === '[') {
        depth += 1;
      } else if (character === '}' || character === ']') {
        depth -= 1;
      } else if (character === '#' && depth === 0 &&
                 (index === 0 || /\s/.test(value[index - 1]))) {
        return value.slice(0, index);
      }
    }
    return value;
  };

  const unquote = value => {
    const trimmed = value.trim();
    if (trimmed.length >= 2 && trimmed.startsWith("'") &&
        trimmed.endsWith("'")) {
      return trimmed.slice(1, -1).replace(/''/g, "'");
    }
    if (trimmed.length >= 2 && trimmed.startsWith('"') &&
        trimmed.endsWith('"')) {
      try {
        return JSON.parse(trimmed);
      } catch (err) {
        return trimmed.slice(1, -1);
      }
    }
    return trimmed;
  };

  const scalar = value => {
    const trimmed = value.trim();
    if (trimmed === '') return null;
    if (/^(null|~)$/i.test(trimmed)) return null;
    if (/^true$/i.test(trimmed)) return true;
    if (/^false$/i.test(trimmed)) return false;
    if (trimmed.startsWith("'") || trimmed.startsWith('"')) {
      return unquote(trimmed);
    }
    return trimmed;
  };

  // Split a block mapping entry at the first colon that is outside quotes and
  // flow collections and followed by whitespace or the end of the line.
  const splitMapping = (line, lineNumber) => {
    let quote = '';
    let depth = 0;
    for (let index = 0; index < line.length; index += 1) {
      const character = line[index];
      if (quote) {
        if (quote === '"' && character === '\\') {
          index += 1;
        } else if (quote === "'" && character === "'" &&
                   line[index + 1] === "'") {
          index += 1;
        } else if (character === quote) {
          quote = '';
        }
        continue;
      }
      if (character === '"' || character === "'") {
        quote = character;
      } else if (character === '{' || character === '[') {
        depth += 1;
      } else if (character === '}' || character === ']') {
        depth -= 1;
      } else if (character === ':' && depth === 0 &&
                 (index + 1 === line.length ||
                  /[ \t]/.test(line[index + 1]))) {
        return {
          key: line.slice(0, index).trim(),
          value: line.slice(index + 1),
        };
      }
    }
    throw new Error(
      `line ${lineNumber}: expected a "key: value" mapping entry`
    );
  };

  const parseFlow = (raw, lineNumber) => {
    let index = 0;
    const problem = message => {
      throw new Error(`line ${lineNumber}: ${message}`);
    };
    const skipSpace = () => {
      while (index < raw.length && /\s/.test(raw[index])) index += 1;
    };
    const parseQuoted = () => {
      const quote = raw[index];
      const start = index;
      index += 1;
      while (index < raw.length) {
        if (quote === '"' && raw[index] === '\\') {
          index += 2;
          continue;
        }
        if (quote === "'" && raw[index] === "'" && raw[index + 1] === "'") {
          index += 2;
          continue;
        }
        if (raw[index] === quote) {
          index += 1;
          return unquote(raw.slice(start, index));
        }
        index += 1;
      }
      problem('unterminated quoted scalar');
    };
    const parseValue = () => {
      skipSpace();
      const character = raw[index];
      if (character === '{') return parseMap();
      if (character === '[') return parseList();
      if (character === '"' || character === "'") return parseQuoted();
      const start = index;
      while (index < raw.length &&
             raw[index] !== ',' && raw[index] !== '}' && raw[index] !== ']') {
        index += 1;
      }
      return scalar(raw.slice(start, index));
    };
    const parseMap = () => {
      const result = Object.create(null);
      index += 1;
      skipSpace();
      if (raw[index] === '}') {
        index += 1;
        return result;
      }
      while (index < raw.length) {
        skipSpace();
        let key;
        if (raw[index] === '"' || raw[index] === "'") {
          key = parseQuoted();
        } else {
          const start = index;
          while (index < raw.length && raw[index] !== ':') index += 1;
          key = unquote(raw.slice(start, index));
        }
        skipSpace();
        if (raw[index] !== ':') problem('expected ":" after a flow map key');
        index += 1;
        if (hasOwn(result, key)) problem(`duplicate key "${key}"`);
        result[key] = parseValue();
        skipSpace();
        if (raw[index] === ',') {
          index += 1;
          continue;
        }
        if (raw[index] === '}') {
          index += 1;
          return result;
        }
        problem('expected "," or "}" in a flow map');
      }
      return problem('unterminated flow map');
    };
    const parseList = () => {
      const result = [];
      index += 1;
      skipSpace();
      if (raw[index] === ']') {
        index += 1;
        return result;
      }
      while (index < raw.length) {
        result.push(parseValue());
        skipSpace();
        if (raw[index] === ',') {
          index += 1;
          continue;
        }
        if (raw[index] === ']') {
          index += 1;
          return result;
        }
        problem('expected "," or "]" in a flow list');
      }
      return problem('unterminated flow list');
    };
    const parsed = parseValue();
    skipSpace();
    if (index !== raw.length) problem('unexpected trailing flow content');
    return parsed;
  };

  const parseValueText = (raw, lineNumber) => {
    const trimmed = stripComment(raw).trim();
    if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
      return parseFlow(trimmed, lineNumber);
    }
    return scalar(trimmed);
  };

  const parseDocument = source => {
    const document = Object.create(null);
    const stack = [{ indent: -1, value: document, kind: 'map' }];
    const lines = source.split('\n');
    let sawContent = false;
    let documentEnded = false;
    for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
      const raw = lines[lineIndex];
      if (raw.trim() === '') continue;
      const lineNumber = lineIndex + 1;
      const indentText = leadingIndent(raw);
      if (indentText.includes('\t')) {
        throw new Error(`line ${lineNumber}: tab indentation is not YAML`);
      }
      const indent = indentText.length;
      const content = raw.slice(indent);
      if (content.startsWith('#')) continue;
      // GitHub rejects multi-document workflow files, so content after a
      // document-end marker or a second document-start marker is invalid even
      // when a later document would declare workflow_dispatch.
      const documentStart = /^---(?:\s|$)/.test(content);
      const documentEnd = /^\.\.\.(?:\s|$)/.test(content);
      if ((documentStart || documentEnd) && indent === 0) {
        if (documentEnd) {
          if (documentEnded) {
            throw new Error(`line ${lineNumber}: duplicate document end`);
          }
          documentEnded = true;
          continue;
        }
        if (sawContent || documentEnded) {
          throw new Error(
            `line ${lineNumber}: multi-document YAML is not allowed`
          );
        }
        continue;
      }
      if (documentEnded) {
        throw new Error(
          `line ${lineNumber}: multi-document YAML is not allowed ` +
          `(content after the document end)`
        );
      }
      sawContent = true;
      const item = content === '-' || content.startsWith('- ');
      if (item) {
        // A block sequence may sit at its key's indentation or deeper. The
        // placeholder created for the empty value is still empty, so turn it
        // into an array before the generic unwind pops it.
        const current = stack[stack.length - 1];
        if (current && current.kind === 'map' && current.parentMap &&
            Object.keys(current.value).length === 0 &&
            indent >= current.indent) {
          const values = [];
          current.parentMap[current.key] = values;
          current.kind = 'seq';
          current.value = values;
          // Items define the sequence's own indentation; a key may introduce
          // a sequence either at its indentation or one level deeper.
          current.indent = indent;
        }
      }
      while (stack.length > 1 &&
             indent <= stack[stack.length - 1].indent) {
        const top = stack[stack.length - 1];
        if (item && top.kind === 'seq' && indent === top.indent) break;
        stack.pop();
      }
      const parent = stack[stack.length - 1];
      if (parent && parent.kind === 'seq') {
        // Only an item at the sequence's own indentation is a new element.
        // Continuation lines belong to the current item; tracking them as
        // mapping siblings would invent duplicate keys in valid workflows
        // such as strategy.matrix.include or repeated step `with:` blocks.
        if (!item || indent !== parent.indent) continue;
        const itemText = content === '-' ? '' : content.slice(2).trim();
        parent.value.push(
          itemText === '' ? null : parseValueText(itemText, lineNumber)
        );
        continue;
      }
      if (item) continue;
      if (!parent || parent.kind !== 'map') continue;
      const entry = splitMapping(content, lineNumber);
      const key = unquote(entry.key);
      if (!key) throw new Error(`line ${lineNumber}: empty mapping key`);
      if (hasOwn(parent.value, key)) {
        throw new Error(`line ${lineNumber}: duplicate key "${key}"`);
      }
      const valueText = stripComment(entry.value).trim();
      if (/^[|>][-+]?[0-9]*$/.test(valueText)) {
        parent.value[key] = '';
        while (lineIndex + 1 < lines.length) {
          const next = lines[lineIndex + 1];
          if (next.trim() === '') {
            lineIndex += 1;
            continue;
          }
          if (leadingIndent(next).length <= indent) break;
          lineIndex += 1;
        }
        continue;
      }
      if (valueText === '') {
        const child = Object.create(null);
        parent.value[key] = child;
        stack.push({
          indent,
          value: child,
          kind: 'map',
          parentMap: parent.value,
          key,
        });
        continue;
      }
      parent.value[key] = parseValueText(valueText, lineNumber);
    }
    return document;
  };

  const source = String(text || '').replace(/^﻿/, '').replace(/\r\n?/g, '\n');
  let document;
  try {
    document = parseDocument(source);
  } catch (err) {
    return fail(`${label} is not valid YAML: ${err.message}`);
  }

  let dispatch;
  const onValue = hasOwn(document, 'on') ? document.on : undefined;
  if (Array.isArray(onValue)) {
    dispatch = onValue.some(value => String(value) === 'workflow_dispatch')
      ? Object.create(null)
      : undefined;
  } else if (typeof onValue === 'string') {
    dispatch = onValue.trim() === 'workflow_dispatch'
      ? Object.create(null)
      : undefined;
  } else if (onValue && typeof onValue === 'object' &&
             hasOwn(onValue, 'workflow_dispatch')) {
    dispatch = onValue.workflow_dispatch;
  }
  if (dispatch === undefined) {
    return fail(`${label} does not declare workflow_dispatch`);
  }
  if (dispatch !== null && typeof dispatch !== 'object') {
    return fail(`${label} has an invalid workflow_dispatch declaration`);
  }

  let inputs = Object.create(null);
  if (dispatch && hasOwn(dispatch, 'inputs') && dispatch.inputs !== null) {
    if (typeof dispatch.inputs !== 'object' ||
        Array.isArray(dispatch.inputs)) {
      return fail(`${label} declares invalid workflow_dispatch inputs`);
    }
    inputs = dispatch.inputs;
  }

  const declared = Object.keys(inputs);
  const supplied = [...new Set(
    (Array.isArray(requiredInputs) ? requiredInputs : [])
      .map(value => String(value))
  )];
  const missing = supplied.filter(name => !hasOwn(inputs, name));
  if (missing.length > 0) {
    return fail(
      `${label} does not declare required input(s): ${missing.join(', ')}`,
      declared
    );
  }

  const requiredWithoutDefault = [];
  const requiredNames = [];
  for (const name of declared) {
    const metadata = inputs[name];
    if (metadata === null || metadata === undefined) continue;
    if (typeof metadata !== 'object' || Array.isArray(metadata)) {
      return fail(`${label} input "${name}" must be a mapping`, declared);
    }
    if (!hasOwn(metadata, 'required') || metadata.required === null) continue;
    const required = metadata.required;
    if (required !== true && required !== false &&
        required !== 'true' && required !== 'false') {
      return fail(
        `${label} input "${name}" has a non-boolean required value`,
        declared
      );
    }
    if (required === true || required === 'true') {
      requiredNames.push(name);
      if (!hasOwn(metadata, 'default') && !supplied.includes(name)) {
        requiredWithoutDefault.push(name);
      }
    }
  }
  if (requiredWithoutDefault.length > 0) {
    return fail(
      `${label} requires input(s) without a default that the dispatch does ` +
      `not send: ${requiredWithoutDefault.join(', ')}`,
      declared
    );
  }
  return { ok: true, inputs: declared, required: requiredNames, supplied };
}
// END continuum-workflow-dispatch-contract

module.exports = { parseWorkflowDispatchContract };
