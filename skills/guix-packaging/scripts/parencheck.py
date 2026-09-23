#!/usr/bin/env python3
r"""Report which top-level form in a Scheme file is unbalanced.

Usage: parencheck.py FILE.scm

Tracks paren depth while skipping strings, line comments, block comments
(#| |#) and character literals such as #\( so they do not count.  Every
column-zero '(' should start at depth 0; when it does not, the form before
it is the unbalanced one, and the report says by how many parens.  Guile's
own error for the same mistake only says "unexpected end of input".
"""
import sys


def check(text: str) -> list[str]:
    depth = 0
    i = 0
    line = 1
    col = 0
    in_string = False
    in_comment = False
    block_comment = 0
    last_form = None
    report = []

    while i < len(text):
        ch = text[i]
        if ch == "\n":
            line += 1
            col = 0
            in_comment = False
            i += 1
            continue
        if in_comment:
            i += 1
            col += 1
            continue
        if block_comment:
            if text.startswith("|#", i):
                block_comment -= 1
                i += 2
                col += 2
                continue
            if text.startswith("#|", i):
                block_comment += 1
                i += 2
                col += 2
                continue
            i += 1
            col += 1
            continue
        if in_string:
            if ch == "\\":
                i += 2
                col += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            col += 1
            continue
        if ch == '"':
            in_string = True
        elif ch == ";":
            in_comment = True
        elif text.startswith("#|", i):
            block_comment = 1
            i += 2
            col += 2
            continue
        elif text.startswith("#\\", i):
            i += 3
            col += 3
            while i < len(text) and text[i].isalpha():
                i += 1
                col += 1
            continue
        elif ch == "(":
            if col == 0:
                if depth != 0:
                    report.append(
                        f"line {line}: depth {depth} on entry; the form starting "
                        f"at line {last_form} is unbalanced by {depth}")
                    depth = 0
                last_form = line
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
        col += 1

    if depth != 0:
        report.append(
            f"end of file: depth {depth}; the form starting at line {last_form} "
            f"is unbalanced by {depth}")
    return report


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    report = check(open(sys.argv[1]).read())
    if not report:
        print("balanced")
        return 0
    for line in report:
        print(line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
