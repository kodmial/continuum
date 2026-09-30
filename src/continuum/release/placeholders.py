"""Unfilled template placeholders in generated files.

A generated manifest is produced by substituting a version, a digest and a set of
maintainers into a template. When a substitution is missed the file is still
syntactically fine and still looks plausible, and it ships a literal
``__VERSION__`` to every user who installs from it. So generated files are scanned
for the tokens the template declares as unfilled before they are offered to
anything downstream.

The part that needs care is that a template *documents* its own placeholders. Ruby,
shell, YAML and Portfile all carry examples like ``# substituted for __VERSION__``
or a heredoc that names the token, and a naive substring scan reads those comments
as unfilled work and blocks a release that is completely correct. So the scanner
ignores comment syntax: a placeholder named in a comment is documentation, and a
placeholder on a line that does anything is a defect.

Deliberately a reader of text, not of any particular language. The adapters emit
Ruby, Portfile, shell and YAML, and they are not going to stay that set, so what
this module knows is *how to tell a comment from a line* rather than how to parse
any of them.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, Iterable, Mapping, Sequence, Tuple

__all__ = [
    "COMMENT_SYNTAX",
    "UnfilledPlaceholder",
    "comment_stripped",
    "find_unfilled",
    "suffix_of",
]


#: Comment syntax by file suffix. A suffix maps to (line prefixes, block pairs),
#: where a block pair is (open, close) and may be absent.
COMMENT_SYNTAX: Dict[str, Tuple[Tuple[str, ...], Tuple[Tuple[str, str], ...]]] = {
    # Ruby, shell, Portfile, YAML, TOML, Python, Makefiles, Dockerfile.
    ".rb": (("#",), ()),
    ".sh": (("#",), ()),
    ".bash": (("#",), ()),
    ".zsh": (("#",), ()),
    ".yml": (("#",), ()),
    ".yaml": (("#",), ()),
    ".toml": (("#",), ()),
    ".py": (("#",), ()),
    ".mk": (("#",), ()),
    ".rb.in": (("#",), ()),
    # Portfile and the other packaging descriptors are Ruby or make with the
    # same convention, and their extension is not a language name.
    "portfile": (("#",), ()),
    "makefile": (("#",), ()),
    # JavaScript, TypeScript, Go, Rust, C, Java, Swift.
    ".js": (("//",), (("/*", "*/"),)),
    ".mjs": (("//",), (("/*", "*/"),)),
    ".ts": (("//",), (("/*", "*/"),)),
    ".go": (("//",), (("/*", "*/"),)),
    ".rs": (("//",), (("/*", "*/"),)),
    ".c": (("//",), (("/*", "*/"),)),
    ".h": (("//",), (("/*", "*/"),)),
    ".java": (("//",), (("/*", "*/"),)),
    ".swift": (("//",), (("/*", "*/"),)),
    # XML and HTML.
    ".xml": ((), (("<!--", "-->"),)),
    ".html": ((), (("<!--", "-->"),)),
    ".plist": ((), (("<!--", "-->"),)),
}

#: Applied when the suffix is unknown. Only the ``#`` convention, because
#: guessing a block-comment syntax for a language whose keywords appear in
#: ordinary strings would delete code from the scan and hide a real defect.
_FALLBACK: Tuple[Tuple[str, ...], Tuple[Tuple[str, str], ...]] = (("#",), ())


def suffix_of(name: str) -> str:
    """The key to look up in :data:`COMMENT_SYNTAX` for a file name.

    Lowercased, and returned in the dotted form the table uses. When the final
    component is not a comment convention on its own the last two are joined, so
    ``nanodictate.rb.in`` finds the Ruby entry rather than an ``.in`` entry that
    does not exist.
    """

    base = str(name or "").strip().lower().rstrip("/").rsplit("/", 1)[-1]
    if base in COMMENT_SYNTAX:
        return base
    parts = base.split(".")
    if len(parts) < 2:
        return base
    dotted = "." + parts[-1]
    if dotted in COMMENT_SYNTAX:
        return dotted
    two = "." + ".".join(parts[-2:])
    return two if two in COMMENT_SYNTAX else dotted


def _syntax_for(name: str) -> Tuple[Tuple[str, ...], Tuple[Tuple[str, str], ...]]:
    return COMMENT_SYNTAX.get(suffix_of(name), _FALLBACK)


def _strip_blocks(line: str, blocks: Sequence[Tuple[str, str]], state: Dict[str, bool]) -> str:
    """Remove block comments from one line, carrying an open block across lines.

    The carry is what makes ``/* ... */`` spanning lines work: the opening line's
    text after the opener is dropped, and so is the next line's text before the
    closer. Both are documentation by definition.
    """

    out: list = []
    index = 0
    length = len(line)
    while index < length:
        for opener, closer in blocks:
            if state.get(opener):
                end = line.find(closer, index)
                if end == -1:
                    return "".join(out)
                index = end + len(closer)
                state[opener] = False
                break
            start = line.find(opener, index)
            if start != -1:
                out.append(line[index:start])
                index = start + len(opener)
                state[opener] = True
                break
        else:
            out.append(line[index:])
            return "".join(out)
    return "".join(out)


def _strip_line_comment(line: str, prefixes: Sequence[str]) -> str:
    """Remove the line comment from one line, if it is not inside a string.

    A ``#`` inside a quoted string is content, not a comment. ``url.split("#")``
    and ``"#" * 8`` are ordinary code, and stripping them would let an unfilled
    placeholder hide behind a quote the scanner itself broke.
    """

    quote = ""
    index = 0
    length = len(line)
    while index < length:
        character = line[index]
        if quote:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = ""
        elif character in "\"'":
            quote = character
        elif character == "#" and prefixes and "#" in prefixes:
            return line[:index]
        index += 1
    return line


def comment_stripped(text: str, *, syntax: Tuple[Tuple[str, ...], Tuple[Tuple[str, str], ...]] = _FALLBACK) -> Tuple[Tuple[str, ...], ...]:
    """Return one comment-free line per input line, same length as the input.

    Kept public and line-preserving so a caller can attribute an offence to the line
    it actually came from. Index alignment is the whole reason this is not a single
    regex over the joined text.
    """

    prefixes, blocks = syntax
    state: Dict[str, bool] = {}
    return tuple(
        _strip_line_comment(_strip_blocks(line, blocks, state), prefixes)
        for line in str(text or "").splitlines()
    )


@dataclasses.dataclass(frozen=True)
class UnfilledPlaceholder:
    """One template token found on a line that does something."""

    token: str
    #: 1-based, and the line number in the file as written.
    line: int
    #: The offending line with its comment removed, so the message shows the code
    #: rather than the documentation beside it.
    text: str

    def describe(self) -> Dict[str, object]:
        return {"token": self.token, "line": self.line, "text": self.text}

    def __str__(self) -> str:
        return "{} on line {}: {}".format(self.token, self.line, self.text.strip())


def find_unfilled(text: str, tokens: Iterable[str], *, name: str = "") -> Tuple[UnfilledPlaceholder, ...]:
    """Every unfilled placeholder in ``text``, ignoring the ones in comments.

    ``name`` selects comment syntax by file suffix; without it only ``#`` is
    recognised as a line comment, because a block-comment syntax guessed for an
    unknown language would strip code rather than comments.
    """

    syntax = _syntax_for(name) if name else _FALLBACK
    wanted = sorted({str(token) for token in tokens if str(token)})
    if not wanted:
        return ()
    found: list = []
    for number, line in enumerate(comment_stripped(text, syntax=syntax), start=1):
        for token in wanted:
            if token in line:
                found.append(UnfilledPlaceholder(token=token, line=number, text=line))
                break
    return tuple(found)


def unresolved(path: str, text: str, tokens: Iterable[str]) -> Tuple[str, ...]:
    """The distinct tokens left unfilled in one generated file, for a summary.

    Separated from :func:`find_unfilled` so the caller does not have to deduplicate
    before it can say what is wrong.
    """

    return tuple(sorted({item.token for item in find_unfilled(text, tokens, name=path)}))