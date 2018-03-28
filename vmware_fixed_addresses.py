#!/usr/bin/env python3
"""
Helper for generating `fixed-address` blocks.

Initially created for VMWare Fusion on macOS, but most parts are generic
and should work on other (workstation) VMWare products too.

See README.md (and possibly other scripts in this repo) for instructions
on how to reload/restart the related services.

For now there is no full automation.

This script uses:
* `/Library/Preferences/VMware Fusion/networking`
* `/Library/Preferences/VMware Fusion/vmnet1/dhcpd.conf`
* `/Library/Preferences/VMware Fusion/vmnet8/dhcpd.conf`
* `~/Documents/Virtual Machines.localized/*.vmwarevm/*.vmx`

Does NOT use:
* `/var/db/vmware/vmnet-dhcpd-vmnetN.leases` lease files
* `/Application/VMWare Fusion.app/Contents/Library/` scripts

Default VMWare network names:
* vmnet1 aka "hostonly": uses DHCP but doesn't connect to external networks
* vmnet8 aka "nat": external access via NAT (this is the default)
* "custom" and various bridged modes: not that useful here

This script tries to be smart and find an unused address block for
static addresses. This usually ends up being x.y.z.64/26, as VMWare
reserves .128 -> .255 for the dynamic address range.
"""
import argparse
import fnmatch
import glob
import ipaddress
import json
import logging
import os
import platform
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime


# Note: not sure if these can be changed. In theory could be also guessed from networks file.
NETWORKS = {
    "hostonly": "vmnet1",
    "nat": "vmnet8",
}

# perhaps include the vmx path here... or not...
TEMPLATE_DHCPD_HOST = """# {host} {extra}
host {host} {{
    hardware ethernet {mac};
    fixed-address {ip};
}}
"""

# VMWare base directories by operating system (using platform.system())
# So far only tried out on VMWare Fusion / macOS
SYSTEM = platform.system()
if SYSTEM == "Darwin":
    PREFS_DIR = "/Library/Preferences/VMWare Fusion"
    VM_DIR = os.path.expanduser("~/Documents/Virtual Machines.localized")
else:
    PREFS_DIR = None
    VM_DIR = None


class VirtualMachine:
    def __init__(self, vmx_path):
        self.vmx_path = vmx_path
        with open(vmx_path) as vmx:
            self.info = parse_vmx(vmx, parse_uuid=True)
        if 'displayName' in self.info:
            self.hostname = self.info['displayName']
        else:
            self.hostname = os.path.splitext(os.path.basename(vmx_path))[0]

    def dhcpd_static_block(self, *, ip, mac, **extra):
        extra = " ".join("{}={}".format(k, v) for k, v in sorted(extra.items()))
        return TEMPLATE_DHCPD_HOST.format(host=self.hostname, ip=ip, mac=mac, extra=extra)


def parse_dhcpd_conf(stream):
    """Hacky parser for VMWare DHCPD config files.
    Barely enough to get the subnetworks and some of the reserved addresses out.
    Luckily VMWare config files are usually very similar to each other.
    For a proper parser, either pyparsing or ply should be used. There are some
    existing ISC style config file parsers, check those too...
    """
    re_section = {
        # subnet 192.168.194.0 netmask 255.255.255.0 {
        'subnet': re.compile(r'^\s*subnet\s+(?P<net>[0-9.]+)\s+netmask\s+(?P<mask>[0-9.]+)\s+{\s*'),
        # other sections to be added here...
    }
    re_line = re.compile(r'^\s*[^;]+\s*;\s*$')
    re_end = re.compile(r'^\s*}\s*$')
    section = None
    results = {}
    for line in stream:
        line = line.rstrip()
        if section is None:
            for name, regex in re_section.items():
                match = regex.match(line)
                if match:
                    section = name
                    mdict = match.groupdict()
                    if section == "subnet":
                        mdict["network"] = ipaddress.ip_network((mdict["net"], mdict["mask"]))
                        mdict["reserved"] = set()
                    results[name] = mdict
                    break
        elif re_line.match(line):
            parts = line.strip().rstrip(";").split()
            ptr = results[section]
            if section == "subnet":
                if parts[0] == "range":
                    # mark the range as reserved addresses (note: not a full network, just part of it)
                    ptr["reserved"].update(
                        ipaddress.summarize_address_range(
                            ipaddress.ip_address(parts[1]), ipaddress.ip_address(parts[2])
                        )
                    )
                elif parts[0] == "option":
                    if parts[1] in ("broadcast-address", "domain-name-servers", "netbios-name-servers", "routers"):
                        # Note: everything in the set are _networks_ with single address,
                        # in order to make .overlap() calls work later
                        ptr["reserved"].update(ipaddress.ip_network(x) for x in parts[2:])
                    elif parts[1] == "domain-name":
                        ptr["domain"] = parts[2].strip('"')
            elif section == "host":
                # parse existing fixed-address entries?
                pass
        elif re_end.match(line):
            section = None
    return results


def parse_vmx(stream, *, parse_uuid=False):
    """Parse single VMX file into dictionary
    So far only used for networking related operations
    """
    re_eth = re.compile(r'^((?P<section>[^.]*)\.|)(?P<key>[^ ]+) = "(?P<value>[^"]+)"$')
    sections = defaultdict(dict)
    for line in stream:
        match = re_eth.match(line)
        if not match:
            continue

        section = match.group('section')
        key = match.group('key')
        value = match.group('value')

        if value == "TRUE":
            # ethernet0.present = "TRUE"
            value = True
        elif value == "FALSE":
            # floppy0.present = "FALSE"
            value = False
        elif section == "uuid" and parse_uuid:
            # uuid.bios = "de ad be ef de ad be ef-01 23 45 67 89 ab cd ef"
            value = uuid.UUID(value.replace(" ", ""))
        else:
            try:
                # ehci:0.parent = "-1"
                # memsize = "2048"
                value = int(value, 10)
            except ValueError:
                pass
        if section is None:
            # displayName = "my-guest"
            sections[key] = value
        else:
            # .encoding = "UTF-8"
            # ethernet0.connectionType = "nat"
            # ethernet0.generatedAddress = "00:0C:29:AA:BB:CC"
            sections[section][key] = value
    return dict(sections)


def parse_networking(stream):
    """Parse VMWare `networking` file into dictionary structure"""
    re_answer = re.compile(r'^answer VNET_(?P<num>[0-9]+)_(?P<key>[^ ]+) (?P<value>.*)$')
    ret = defaultdict(dict)
    for line in stream:
        match = re_answer.match(line)
        # there might be other lines too, especially if any bridged networks are in use
        if not match:
            continue
        vmnet = "vmnet{}".format(match.group('num'))
        key = match.group('key').lower()
        value = match.group('value')
        # for 'dhcp', 'nat', 'virtual_adapter', ...
        if value == 'yes':
            value = True
        elif value == 'no':
            value = False   # XXX: not seen in the wild, maybe the lines are usually just missing...

        ret[vmnet][key] = value

    # add network object
    for info in ret.values():
        if 'hostonly_netmask' in info and 'hostonly_subnet' in info:
            info['network'] = ipaddress.ip_network((info['hostonly_subnet'], info['hostonly_netmask']))

    return dict(ret)


def find_subnet(network, reserved):
    """Find biggest sub-network which does not overlap with any of the reserved networks/addresses"""
    for prefix_len in range(network.prefixlen+1, network.max_prefixlen-1):
        for subnet in network.subnets(new_prefix=prefix_len):
            if all(not subnet.overlaps(x) for x in reserved):
                return subnet


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser()
    parser.add_argument("--prefs-dir", default=PREFS_DIR)
    parser.add_argument("--vm-dir", default=VM_DIR)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    # parser.add_argument("--force-network") eg. vmnet8 for all machines
    # --vmx: one or more explicit vmx files or directories containing vmx
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    fname = os.path.join(args.prefs_dir, "networking")
    logging.info("Reading VMWare networking config %r", fname)
    with open(fname) as nconf:
        networks = parse_networking(nconf)
    logging.info("Found networks: %s", " ".join(sorted(networks)))

    for vmnet, info in networks.items():
        if info['dhcp'] is not True:
            logging.debug("Ignoring non-DHCP network %s %r", vmnet, info)
            continue
        fname = os.path.join(args.prefs_dir, vmnet, "dhcpd.conf")
        logging.info("Reading %s DHCP config %r", vmnet, fname)
        with open(fname) as dconf:
            ret = parse_dhcpd_conf(dconf)
            dhcp_net = ret["subnet"]["network"]
            reserved = ret["subnet"]["reserved"]
            info["domain"] = ret["subnet"].get("domain", "localdomain")

        if info["network"] != dhcp_net:
            logging.warning("%s network / dhcpd config mismatch %s %s", vmnet, net, dhcp_net)

        # XXX: might later on add items to reserved set here

        info['network_fixed'] = find_subnet(dhcp_net, reserved)
        logging.info("Using %s as %s fixed-address range", dhcp_net, vmnet)

    # TODO: mode for specific .vmx paths
    vms = []
    for fname in glob.glob(os.path.join(args.vm_dir, "*", "*.vmx")):
        logging.info("Reading VMX info %r", fname)
        vms.append(VirtualMachine(fname))

    # TODO: create also ssh config, hosts file snippets (avoid duplicates, if machine has both vmnet1 and vmnet8)
    dhcp_confs = defaultdict(list)
    for vm in sorted(vms, key=lambda x: x.hostname):
        for section, info in sorted(vm.info.items()):
            if not section.startswith("ethernet"):
                continue
            conn_type = info.get("connectionType")
            if conn_type not in NETWORKS:
                logging.debug("%r %r unknown connection type %r", vm.hostname, section, conn_type)
                continue
            vmnet = NETWORKS[conn_type]
            extra = {
                "conn": conn_type,
                "net": vmnet,
                "dev": section,
            }
            mac_address = info["generatedAddress"].lower()
            # Note: new run with changed host list -> different addresses. Might get fixed later.
            fixed_ip = networks[vmnet]["network_fixed"][len(dhcp_confs[vmnet])]
            dhcp_confs[vmnet].append(vm.dhcpd_static_block(mac=mac_address, ip=fixed_ip, **extra))

    now = datetime.now().isoformat(timespec="seconds")
    for vmnet, configs in sorted(dhcp_confs.items()):
        print("## {} {} fixed-address configs ##\n".format(now, vmnet))
        for item in configs:
            print(item)


if __name__ == '__main__':
    main()
