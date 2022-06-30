#!/usr/bin/env python3

import argparse
import os
import re
import shutil
import subprocess
import sys
from typing import List, NamedTuple


def run(cmd, **kwargs):
    if "check" not in kwargs:
        kwargs["check"] = True
    return subprocess.run(cmd, **kwargs)


def output(cmd, **kwargs):
    return run(
        cmd, stdout=subprocess.PIPE, encoding=sys.stdout.encoding, **kwargs
    ).stdout


DIFF_LINE_PATTERN = re.compile("[0-9a-f]+ ([ADMRCU][0-9]*)\t(.+)$")

# Don't include files that are either backported to Cinder 3.8 from CPython
# 3.10 or otherwise not relevant to Cinder 3.10.
EXCLUDED_FILES = {
    ".github/PULL_REQUEST_TEMPLATE.md",
    "Include/internal/pycore_unionobject.h",
    "Lib/test/test_import/data/unwritable/__init__.py",
    "Lib/test/test_import/data/unwritable/x.py",
    "Objects/unionobject.c",
    "README.cpython.rst",
}

EXCLUDED_PATTERN = re.compile(r"^(Misc/NEWS\.d/|Include/pydtrace_).+")


def should_port_file(f):
    return f not in EXCLUDED_FILES and not EXCLUDED_PATTERN.match(f)


class DiffFiles(NamedTuple):
    added: List[str]
    other: List[str]


def get_diff_files(args):
    all_files = output(
        ["git", "diff", "--raw", f"{args.cinder_fork_point}..{args.cinder_head}"]
    ).split("\n")
    added_files = []
    other_files = []
    for line in all_files:
        if line == "":
            continue
        m = DIFF_LINE_PATTERN.search(line)
        if not m:
            raise RuntimeError(f"Unknown diff line '{line}'")
        if m[1] == "A":
            added_files.append(m[2])
        else:
            other_files.append(m[2])
    return DiffFiles(added_files, other_files)


COMMIT_TEMPLATE = """
Initial Cinder 3.10 with cinder-exclusive files from latest cinder/3.8

This commit was generated by running:
Tools/scripts/cinder_310_port.py create_branch \\
    --cinder-head {cinder_head} \\
    --cinder-fork-point {cinder_fork_point} \\
    --upstream-head {upstream_head}

It contains the source tree of CPython 3.10 (exact commit shown above), with
most Cinder-exclusive files copied in. It has Cinder 3.8 and CPython 3.10 as
parent commits, to preserve git history from both upstream files and
Cinder-exclusive files.

It is meant as a starting point for Cinder 3.10, and we intend to port changes
to upstream files in Cinder 3.8 on a feature-by-feature basis on top of this
commit.
"""


def create_port_branch(args):
    run(["git", "checkout", args.upstream_head])

    added_files = list(filter(should_port_file, get_diff_files(args).added))
    run(["git", "checkout", args.cinder_head, *added_files])
    shutil.copy(__file__, "Tools/scripts/cinder_310_port.py")
    run(["git", "add", "Tools/scripts"])
    run(["git", "commit", "-m", "Add new cinder/3.8 files"])
    added_tree = re.search(
        r"^tree ([0-9a-f]+)$",
        output(["git", "show", "--no-patch", "--pretty=raw", "HEAD"]),
        re.MULTILINE,
    )[1]

    merge_commit = output(
        [
            "git",
            "commit-tree",
            added_tree,
            "-p",
            args.cinder_head,
            "-p",
            args.upstream_head,
            "-m",
            COMMIT_TEMPLATE.format(
                cinder_head=args.cinder_head,
                cinder_fork_point=args.cinder_fork_point,
                upstream_head=args.upstream_head,
            ),
        ]
    ).strip()

    run(["git", "branch", "-f", args.branch_name, merge_commit])
    run(["git", "checkout", args.branch_name])


def generate_upstream_patches(args):
    cmd = [
        "git",
        "format-patch",
        "--output-directory",
        args.output,
        f"{args.cinder_fork_point}..{args.cinder_head}",
    ]
    if not args.all:
        cmd.append("--")
        cmd += get_diff_files(args).other
    run(cmd)


def parse_args():
    args = argparse.ArgumentParser()
    subparsers = args.add_subparsers(required=True)

    create = subparsers.add_parser(
        "create_branch", help="Create initial Cinder 3.10 branch locally"
    )
    create.set_defaults(func=create_port_branch)

    create.add_argument(
        "--cinder-head",
        default="origin/cinder/3.8",
        help="Cinder commit to use as the head",
    )
    create.add_argument(
        "--cinder-fork-point",
        default="origin/3.8",
        help="Upstream commit Cinder was forked from",
    )
    create.add_argument(
        "--upstream-head",
        default="origin/3.10",
        help="Upstream commit to merge into Cinder",
    )
    create.add_argument("--branch-name", required=True, help="Name of branch to create")

    patches = subparsers.add_parser(
        "generate_upstream_patches",
        help="Generate a directory of patches from Cinder 3.8",
    )
    patches.add_argument(
        "--output", "-o", required=True, help="Directory to write patches to"
    )
    patches.add_argument(
        "--all",
        "-a",
        action="store_true",
        help="Include all patches, not just patches to upstream files",
    )
    patches.set_defaults(func=generate_upstream_patches)

    return args.parse_args()


def main():
    args = parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
