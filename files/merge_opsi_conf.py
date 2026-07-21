#!/usr/bin/env python3
"""Idempotently merge a managed [ldap_auth] block into /etc/opsi/opsi.conf.
Usage: merge_opsi_conf.py <target_conf> <block_file>
Prints CHANGED or UNCHANGED. Only the block between the ludus_opsi markers is
managed; the rest of opsi.conf is left untouched."""
import sys

START = "# >>> ludus_opsi ldap_auth >>>"
END = "# <<< ludus_opsi ldap_auth <<<"

target, block_path = sys.argv[1], sys.argv[2]
inner = open(block_path).read().strip("\n")
block = START + "\n" + inner + "\n" + END + "\n"

try:
    content = open(target).read()
except FileNotFoundError:
    content = ""

out, skip = [], False
for ln in content.splitlines(keepends=True):
    s = ln.strip()
    if s == START:
        skip = True
        continue
    if s == END:
        skip = False
        continue
    if not skip:
        out.append(ln)

base = "".join(out).rstrip("\n")
new = (base + "\n\n" if base else "") + block
if new != content:
    open(target, "w").write(new)
    print("CHANGED")
else:
    print("UNCHANGED")
