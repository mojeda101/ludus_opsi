#!/usr/bin/env python3
"""opsi-ad-sync - license-free Active Directory -> OPSI client sync.

Replaces the core function of the paid opsi-directory-connector using only
free building blocks: an LDAP read against AD and OPSI's core JSON-RPC API
(host_createOpsiClient / host_getObjects / group_* / objectToGroup_*), which
are the same methods opsi-admin and opsi-cli use.

Reads a JSON config (see --config) and, for every computer object found in the
directory, ensures a matching OpsiClient exists. Optionally derives a host
group from the computer's OU and adds the client to it.

Usage:
    opsi-ad-sync --config /etc/opsi/ad-sync.json [--dry-run]
"""
import argparse
import base64
import json
import ssl
import sys
import urllib.error
import urllib.request

try:
    from ldap3 import Server, Connection, Tls, ALL, SUBTREE, SIMPLE
except ImportError:
    sys.exit("ldap3 is required (apt install python3-ldap3 / pip install ldap3)")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def as_bool(value, default=False):
    """Coerce a config value to bool. Ansible may serialise booleans as the
    strings 'True'/'False' (non-native Jinja), both of which are truthy in
    Python, so parse them explicitly rather than relying on truthiness."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def load_config(path):
    with open(path) as fh:
        return json.load(fh)


def domain_from_search_base(search_base):
    """dc=ludus,dc=domain -> ludus.domain"""
    parts = [p.split("=", 1)[1] for p in search_base.split(",")
             if p.strip().lower().startswith("dc=")]
    return ".".join(parts)


def parse_ldap_address(address):
    """Accept ldap://host[:port], ldaps://host[:port], host:port, host.
    Returns (host, port, use_ssl)."""
    use_ssl = False
    addr = address.strip()
    if addr.lower().startswith("ldaps://"):
        use_ssl, addr = True, addr[8:]
    elif addr.lower().startswith("ldap://"):
        addr = addr[7:]
    addr = addr.rstrip("/")
    host, _, port = addr.partition(":")
    if port:
        port = int(port)
        if port == 636:
            use_ssl = True
    else:
        port = 636 if use_ssl else 389
    return host, port, use_ssl


# --------------------------------------------------------------------------- #
# LDAP
# --------------------------------------------------------------------------- #
def fetch_computers(cfg):
    d = cfg["directory"]
    host, port, use_ssl = parse_ldap_address(d["address"])
    verify = as_bool(d.get("verify_certificate", False))
    ca = d.get("ca_cert_file") or None

    tls = None
    if use_ssl:
        if verify:
            tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=ca)
        else:
            tls = Tls(validate=ssl.CERT_NONE)

    server = Server(host, port=port, use_ssl=use_ssl, get_info=ALL, tls=tls)
    # Simple bind over LDAPS is the compatible choice: AD accepts a down-level
    # "DOMAIN\\user", a UPN "user@domain", or a full DN, and it avoids NTLM
    # channel-binding resets. Requires LDAPS (which we use) since AD refuses
    # password simple binds over cleartext LDAP.
    conn = Connection(server, user=d["user"], password=d["password"],
                      authentication=SIMPLE, auto_bind=True)

    page = int(d.get("paged_search_limit", 768)) or 768
    conn.search(
        search_base=d["search_base"],
        search_filter=d.get("search_query_computers", "(objectClass=computer)"),
        search_scope=SUBTREE,
        attributes=["name", "dNSHostName", "description", "distinguishedName"],
        paged_size=page,
    )
    entries = list(conn.entries)
    conn.unbind()
    return entries


def value(entry, attr):
    try:
        v = entry[attr].value
    except Exception:
        return ""
    return "" if v is None else str(v)


def client_id_for(entry, default_domain):
    dns = value(entry, "dNSHostName").strip().lower()
    if dns:
        return dns
    name = value(entry, "name").strip().lower()
    return f"{name}.{default_domain}" if default_domain else name


def group_for(entry):
    """First OU= component of the DN -> lowercased group id (or None)."""
    dn = value(entry, "distinguishedName")
    for part in dn.split(","):
        part = part.strip()
        if part.lower().startswith("ou="):
            return part.split("=", 1)[1].strip().lower()
    return None


# --------------------------------------------------------------------------- #
# OPSI JSON-RPC
# --------------------------------------------------------------------------- #
class OpsiRPC:
    def __init__(self, cfg):
        o = cfg["opsi"]
        self.url = o["address"].rstrip("/") + "/rpc"
        cred = f'{o["username"]}:{o["password"]}'.encode()
        self.auth = "Basic " + base64.b64encode(cred).decode()
        self.ctx = ssl.create_default_context()
        if not as_bool(o.get("verify_certificate", False)):
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def call(self, method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Authorization", self.auth)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=60) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method}: HTTP {e.code} {e.reason}")
        if data.get("error"):
            raise RuntimeError(f"{method}: {data['error']}")
        return data.get("result")


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="License-free AD -> OPSI client sync")
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would happen, change nothing")
    args = ap.parse_args()

    cfg = load_config(args.config)
    behaviour = cfg.get("behaviour", {})
    dry = args.dry_run or not as_bool(behaviour.get("write_changes_to_opsi", True), default=True)
    default_domain = (cfg["directory"].get("domain")
                      or domain_from_search_base(cfg["directory"]["search_base"]))
    root = behaviour.get("root_dir_in_opsi", "clientdirectory")
    do_groups = as_bool(behaviour.get("create_groups_from_ou", False))

    entries = fetch_computers(cfg)
    rpc = OpsiRPC(cfg)

    # Resolve the depot to assign clients to. A client is not usable or visible
    # in configed/WebGUI until it has a clientconfig.depot.id, which
    # host_createOpsiClient does not set. "auto" -> the configserver; "" -> skip.
    depot_setting = behaviour.get("depot", "auto")
    depot = None
    if depot_setting:
        if depot_setting == "auto":
            servers = rpc.call("host_getIdents", ["str", {"type": "OpsiConfigserver"}])
            depot = servers[0] if servers else None
        else:
            depot = depot_setting

    existing = {h["id"].lower() for h in
                rpc.call("host_getObjects", [[], {"type": "OpsiClient"}])}
    known_groups = {g["id"].lower() for g in
                    rpc.call("group_getObjects", [[], {"type": "HostGroup"}])}

    created = added = skipped = depoted = 0
    print("---------- opsi actions ----------")
    for e in entries:
        cid = client_id_for(e, default_domain)
        if not cid:
            continue
        descr = value(e, "description")
        if cid.lower() not in existing:
            print(f"Creating client {cid}.")
            if not dry:
                rpc.call("host_createOpsiClient",
                         [cid, None, descr, "", "", "", "", "", None, None])
            existing.add(cid.lower())
            created += 1
        else:
            skipped += 1

        # Assign to depot (idempotent) for both new and existing clients, so
        # already-present clients missing a depot get fixed on the next run.
        if depot:
            print(f"Assigning {cid} to depot {depot}.")
            if not dry:
                rpc.call("configState_create",
                         ["clientconfig.depot.id", cid, [depot]])
            depoted += 1

        if do_groups:
            gid = group_for(e)
            if gid:
                if gid.lower() not in known_groups:
                    print(f"Creating group {gid}.")
                    if not dry:
                        rpc.call("group_createHostGroup",
                                 [gid, f"OU {gid}", "", root])
                    known_groups.add(gid.lower())
                print(f"Adding {cid} to group {gid}.")
                if not dry:
                    rpc.call("objectToGroup_create", ["HostGroup", gid, cid])
                added += 1
    print("----------------------------------")
    print("---------- summary ---------------")
    mode = " (dry-run)" if dry else ""
    print(f"Directory computers found: {len(entries)}{mode}")
    print(f"Created {created} clients, {skipped} already present.")
    print(f"Depot assignments ensured: {depoted}"
          + (f" -> {depot}." if depot else " (none; depot disabled)."))
    print(f"Group memberships ensured: {added}.")
    print("----------------------------------")


if __name__ == "__main__":
    main()
