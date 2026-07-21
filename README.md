# Ansible Role: OPSI ([Ludus](https://ludus.cloud))

> [!CAUTION]
> **Lab use only.** This role is built for [Ludus](https://ludus.cloud) cyber
> ranges and similar throwaway lab environments. It is **not** hardened for
> production: passwords use only alphanumeric characters to avoid shell
> escaping issues, TLS certificate verification is disabled by default, service
> ports are exposed, and SSSD uses `access_provider = permit`. Do not run on
> internet-facing or production systems.

An Ansible role that deploys an [OPSI](https://www.opsi.org) 4.3 configserver
as a **Docker Compose stack** on a Debian/Ubuntu host, with free Active
Directory integration (client sync + admin login via PAM/SSSD) and automatic
Windows agent deployment. **No paid OPSI module required.**

Use together with the companion role **`ludus_opsi_client`**, which runs on
the DC (creates the `opsiadmin` group + DNS A record) and on Windows clients
(pulls and installs the `opsi-client-agent`).

> [!NOTE]
> The Docker variant of OPSI communicates with depots over **WebDAV only** —
> there is no Samba support in this deployment.

## What a full `ludus range deploy` does

On a complete deployment with the example range config, zero manual steps are
required:

| Step | What runs | What it does |
|------|-----------|--------------|
| 1 | `ludus_opsi_client` on DC | Creates `opsiadmin` AD group + DNS A record `opsi.<domain>` |
| 2 | `ludus_opsi` on Linux VM | Sudo safeguard, Docker, SSSD, derived image, stack, products, sync, login |
| 3 | `ludus_opsi_client` on Windows | Pulls agent installer, installs silently, reboots |

The client VMs use `depends_on` so they wait for the server role to finish
before attempting the download — the stack is always up before they connect.

## Requirements

- Debian (bullseye/bookworm) or Ubuntu (focal/jammy/noble) VM in your Ludus
  range with internet access (Docker + OPSI packages pulled at deploy time).
- `ansible.windows` collection on the Ludus server (for the companion role).

## Role Variables

See `defaults/main.yml` for the full list with comments. The most relevant:

```yaml
# --- Stack ---
ludus_opsi_install_dir: /opt/opsi-server
ludus_opsi_image_tag: "4.3"
ludus_opsi_https_port: 4447
ludus_opsi_host_role: configserver      # or "depotserver"

# --- Passwords (alphanumeric ONLY - no ! or special chars, see Known Issues) ---
ludus_opsi_admin_password: "LudusOpsiAdmin1"   # local 'adminuser' fallback

# --- Sudo safeguard ---
ludus_opsi_preserve_local_sudo: true
ludus_opsi_local_sudo_user: "localuser"

# --- Shared AD connection (drives BOTH free sync and free login) ---
ludus_opsi_ad_use_ldaps: false          # Ludus DCs have no LDAPS cert -> plaintext
ludus_opsi_ad_dc_host: "dc01.lab.test"  # resolved via dns_rewrites on the DC VM
ludus_opsi_ad_bind_dn: "domainuser@ludus.domain"
ludus_opsi_ad_bind_password: "password"
ludus_opsi_ad_search_base: "dc=ludus,dc=domain"

# --- Free client sync: AD computers -> OPSI clients (depot-assigned) ---
ludus_opsi_ad_sync_enabled: false
ludus_opsi_ad_sync_groups_from_ou: false
ludus_opsi_ad_sync_depot: "auto"        # auto-resolves the configserver depot

# --- Free AD login: AD users log into OPSI via PAM/SSSD ---
ludus_opsi_ad_login_enabled: false
ludus_opsi_ad_login_admin_group: "opsiadmin"

# --- Base products (opsi-script is a hard dep of the agent - 404 without it) ---
ludus_opsi_install_base_products: true
ludus_opsi_base_products:
  - opsi-script
  - opsi-client-agent
  - opsi-configed
  - hwaudit
  - swaudit

# --- Configserver URL for agents (use hostname so cert validates) ---
ludus_opsi_configserver_url: "https://opsi.ludus.domain:4447/rpc"
```

## Example Ludus Range Config

A complete, schema-validated range config ships as `opsi-ad-range.yml`. Key
points:

- DC uses `dns_rewrites: [dc01.lab.test]` so the Linux OPSI box resolves it
  via the range router (the Linux box doesn't use the DC for DNS).
- Client VMs have `depends_on` pointing at the opsi server's role so they wait
  for the stack to be up before downloading the agent installer.
- All passwords are alphanumeric — no `!` or shell-special characters.

```yaml
ludus:
  - vm_name: "{{ range_id }}-DC01-2022"
    # ... primary-dc ...
    dns_rewrites:
      - dc01.lab.test
    roles:
      - mojeda101.ludus_opsi_client
    role_vars:
      opsi_client_dc: true
      opsi_client_ad_group_members: ["domainadmin"]
      opsi_client_dns_record_ip: "10.16.10.20"

  - vm_name: "{{ range_id }}-opsi"
    # ... debian linux ...
    roles:
      - mojeda101.ludus_opsi
    role_vars:
      ludus_opsi_admin_password: "LudusOpsiAdmin1"
      ludus_opsi_dc_ad_fqdn: "ludus.domain"
      ludus_opsi_ad_use_ldaps: false
      ludus_opsi_ad_dc_host: "dc01.lab.test"
      ludus_opsi_ad_bind_dn: "domainuser@ludus.domain"
      ludus_opsi_ad_bind_password: "password"
      ludus_opsi_ad_search_base: "dc=ludus,dc=domain"
      ludus_opsi_ad_sync_enabled: true
      ludus_opsi_ad_sync_groups_from_ou: true
      ludus_opsi_ad_login_enabled: true
      ludus_opsi_configserver_url: "https://opsi.ludus.domain:4447/rpc"

  - vm_name: "{{ range_id }}-win11"
    # ... windows member ...
    roles:
      - name: mojeda101.ludus_opsi_client
        depends_on:
          - vm_name: "{{ range_id }}-opsi"
            role: mojeda101.ludus_opsi
    role_vars:
      opsi_client_configserver: "https://opsi.ludus.domain:4447"
      opsi_client_service_password: "LudusOpsiAdmin1"
```

## Free AD integration (no paid module)

Both directions of AD integration work without an OPSI modules license:

### Client sync (`ludus_opsi_ad_sync_enabled`)

Reads computer objects from AD over LDAP and creates OpsiClients via the free
OPSI JSON-RPC API (`host_createOpsiClient`, `configState_create`, `group_*`).
Each client is assigned to the configserver depot so it appears in
configed/WebGUI immediately. Runs on a systemd timer (hourly). Idempotent.

```bash
# run manually / dry-run:
opsi-ad-sync --config /etc/opsi/ad-sync.json --dry-run
opsi-ad-sync --config /etc/opsi/ad-sync.json
```

### AD login (`ludus_opsi_ad_login_enabled`)

Lets AD users log into OPSI (admin page / configed / WebGUI) via PAM/SSSD.
SSSD runs on the host against AD (no Kerberos realm join, no LDAPS cert needed)
and is bind-mounted into a thin derived `opsi-server` image. opsiconfd
PAM-authenticates AD users and grants admin to members of `opsiadmin`.

The `opsiadmin` group is created automatically by the companion role's DC mode.

Use **either** the free path **or** the paid `directory_connector` /
`ldap_auth` — not both.

## Post-deploy verification

```bash
CC='docker compose -f /opt/opsi-server/docker-compose.yml exec -T opsi-server'

# stack healthy?
docker compose -f /opt/opsi-server/docker-compose.yml ps

# base products in depot?
$CC bash -lc 'ls -1 /data/lib/depot/ | grep -iE "opsi-script|hwaudit"'

# AD group collision fixed (AD group should have high GID, not 1000)?
$CC getent group opsiadmin

# AD user resolves + group membership correct?
$CC id domainadmin          # must include opsiadmin

# clients synced and phoning home?
$CC opsi-cli jsonrpc execute host_getObjects '[]' '{"type":"OpsiClient"}' \
  | grep -iE '"id"|lastSeen|ipAddress'

# push a product to a connected client:
$CC opsi-cli client-action --clients mom-win11.ludus.domain \
  set-action-request --products hwaudit
$CC opsi-cli client-action --clients mom-win11.ludus.domain process-actions
```

Login to `https://<opsi-ip>:4447/admin` as `domainadmin` (AD) or `adminuser`
(local fallback, password = `ludus_opsi_admin_password`). The WebGUI is at
`/addons/webgui/app/` — clear browser cache if you see a stale session after
the first deploy.

## Known issues (and how the role handles them)

**No `!` in passwords.** Docker Compose passes `OPSI_ADMIN_PASSWORD` through a
shell context during container init — `!` triggers bash history expansion and is
silently dropped, leaving the stored password wrong. If you get
`Authentication error` after a clean deploy this is the cause. All role
defaults use alphanumeric passwords only. If the `opsi_data` volume has a
wrong password (e.g. from a previous deploy with `!`), wipe it:
```bash
cd /opt/opsi-server && docker compose down
docker volume rm opsi-server_opsi_data
docker compose up -d
```

**`opsi_data` volume persists passwords.** `OPSI_ADMIN_PASSWORD` only takes
effect on first volume initialisation. Changing the password in the compose
file and restarting the container does NOT reset the stored password. Always
wipe the volume to change the admin password.

**Local `opsiadmin` group collision.** The `opsi-server` image ships a local
`opsiadmin` group (gid 1000, member `opsiconfd`). The role renames it to
`opsiadmin-local` inside the container so the AD group wins the name lookup.
This runs automatically in `base_products.yml` after the stack is up,
idempotent (only renames if gid 1000 still present).

**opsiclientd SSL cert mismatch.** If you set `clientconfig.configserver.url`
to the server's IP address, opsiclientd rejects the connection with
`certificate is not valid for '10.x.x.x'` (IP not in cert SAN). Always use
the hostname (`opsi.ludus.domain`) — the companion role creates the DNS A record
automatically so domain-joined clients resolve it.

**OPSI server-side push needs DCOM/WMI.** `opsi-deploy-client-agent` uses
DCOM/WMI (RPC port 135) in addition to SMB — `0x800706ba` means DCOM is
blocked by the Windows client firewall. The companion role uses a pull install
instead (client downloads from `https://<server>:4447/public/opsi-client-agent/`)
which needs only outbound HTTPS. Set `opsi_client_enable_wmi_firewall: true`
in the companion role if you want to test server-side push.

**Client role must wait for server.** If `ludus_opsi_client` runs on a Windows
VM before `ludus_opsi` finishes on the server, the installer download fails
with `Unable to connect`. Use `depends_on` in the range config (see the example).

**SSSD plaintext LDAP.** Standard Ludus Windows DCs are promoted without AD
Certificate Services, so port 636 (LDAPS) is dead. Set `ludus_opsi_ad_use_ldaps:
false` to use plaintext port 389. Bind + auth traffic is unencrypted — fine
inside a throwaway range, never beyond it.

## License

MIT (role). OPSI itself is AGPL/uib

## Author Information

Created by [mojeda101](https://github.com/mojeda101) for [Ludus](https://ludus.cloud/).
