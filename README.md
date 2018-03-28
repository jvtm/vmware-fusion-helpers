# Helper scripts for managing VMWare Fusion

Collection of scripts and tools for managing VMWare Fusion and guest operating systems running on it.

As this is for a desktop product, there is no full automation nor any extensive tests.

All tools here are [MIT](LICENSE.txt) licensed (unless stated otherwise, eg. script derived from other sources).

Some of the tools might work on VMWare (desktop) products on other platforms too.

## `vmware_fixed_addresses.py`

Script for parsing network info, dhcpd.configs and VMX files in order to generate `fixed-address` blocks.

Looks like modern Linux distributions have moved back to actively releasing DHCP leases,
and therefore getting a different IP address on every suspend/resume.
This script can be used for generating ISC DHCPD fixed-address blocks for all local virtual machines.

Works for both `vmnet8` aka "nat" and `vmnet1` aka "hostonly" virtual machines.

There are (or were) some plans to make this more automatic and also modifying configs, restarting daemons etc.
At least instructions might follow soon (after some more real life tests).

For now you need to modify the `dhcpd.conf` files manually (running the script will tell you the locations tho)

To restart DHCP daemons after modifications:

    $ sudo /Applications/VMware\ Fusion.app/Contents/Library/services/services.sh --stopdaemons
    $ sudo /Applications/VMware\ Fusion.app/Contents/Library/services/services.sh --start


## `vmware_mem_adjust.py`

A lost utility on balancing virtual machine memory settings based on amount of memory available on host + taking all local virtual machines into account. Modifies `.vmx` files in place.

*Not imported to this repository yet...*

