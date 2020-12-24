import ipaddress

import click
from click_spinner import spinner
from tabulate import tabulate

from .base import BaseCommand


def log(msg):
    click.echo(msg, err=True)


@click.group()
def network():
    pass


class NetworkDeleteCommand(BaseCommand):
    @staticmethod
    @network.command(name='delete')
    @click.option('--segment', 'segment', help='network segment (VLAN) ID')
    @click.option('--network', 'network', help='network ID')
    def cli(segment, network):
        """Tear down a network, including routers and ports.
        """
        return NetworkDeleteCommand().run(segment=segment, network=network)

    def run(self, segment=None, network=None):
        neutron = self.neutron()

        if segment is not None:
            neutron_network = self._find_network(neutron, {
                "provider:segmentation_id": segment
            })
        elif network is not None:
            neutron_network = neutron.get_network(network)
        else:
            raise ValueError("Missing either segment ID or network ID")

        # Find ports
        ports = self._list_for_network(neutron, neutron_network, "ports")

        # Abort if there are nova ports, this means there are running instances
        if any(p.get("device_owner") == "compute:nova" for p in ports):
            raise ValueError("Network has running instances!")

        router_ports = [
            p for p in ports
            if p["device_owner"] == "network:router_interface"
        ]

        # Detach subnets from router(s)
        for p in router_ports:
            self.log.info("Deleting router interface {}".format(p["id"]))
            neutron.remove_interface_router(p["device_id"], {
                "port_id": p["id"]
            })

        subnets = self._list_for_network(neutron, neutron_network, "subnets")

        for s in subnets:
            self.log.info("Deleting subnet {}".format(s["id"]))
            neutron.delete_subnet(s["id"])

        self.log.info("Deleting network {}".format(neutron_network["id"]))
        neutron.delete_network(neutron_network["id"])

        for p in router_ports:
            router_id = p["device_id"]
            other_router_ports = neutron.list_ports(
                device_id=router_id,
                device_owner=p["device_owner"]
            ).get("ports")
            if not other_router_ports:
                self.log.info("Removing router gateway")
                neutron.remove_gateway_router(router_id)
                self.log.info("Removing router {}".format(router_id))
                neutron.delete_router(router_id)

    def _find_network(self, neutron, params):
        networks = neutron.list_networks(**params).get("networks")

        if not networks:
            raise ValueError("Could not find network for {}".format(params))

        return networks[0]

    def _list_for_network(self, neutron, network, name):
        network_id = network.get("id")
        getter = getattr(neutron, "list_{}".format(name))
        return getter(network_id=network_id).get(name)


class NetworkSegmentStatusCommand(BaseCommand):
    @staticmethod
    @network.command(name='segments')
    def cli():
        """Display the current Neutron networks assigned for each VLAN.

        The name of the network and its owning project are also displayed.
        """
        return NetworkSegmentStatusCommand().run()

    def run(self):
        neutron = self.neutron()
        networks = neutron.list_networks().get("networks")

        rows = []
        rows.append([
            "physical_network",
            "segmentation_id",
            "name",
            "project_id"
        ])

        def sort_key(x):
            # Handle networks without a VLAN tag, which have no segment ID
            return x.get("provider:segmentation_id") or -1

        for n in sorted(networks, key=sort_key):
            rows.append([
                n["provider:physical_network"],
                str(n["provider:segmentation_id"]),
                n["name"],
                n["project_id"]
            ])

        widths = [max(map(len, col)) for col in zip(*rows)]
        for row in rows:
            cols = (val.ljust(width) for val, width in zip(row, widths))
            print("  ".join(cols))


class NetworkPublicIPStatusCommand(BaseCommand):
    @staticmethod
    @network.command(name='ips')
    def cli():
        """Check the status of public IP addresses.

        Displays all public IP addresses in the 'public' network DHCP range
        and their current allocation status (as Floating IP or router gateway),
        if any.
        """
        return NetworkPublicIPStatusCommand().run()

    def _public_allocation_pools(self, neutron):
        networks = neutron.list_networks(name="public").get("networks")
        if not networks:
            raise ValueError("Could not find public network")

        public_net = networks[0]
        subnets = public_net.get("subnets")
        if not subnets:
            raise ValueError("No subnets defined on public network")

        allocation_pools = []
        for subnet_id in subnets:
            subnet = neutron.show_subnet(subnet_id).get("subnet")
            allocation_pools.extend(subnet.get("allocation_pools"))

        return allocation_pools

    def run(self):
        blazar = self.blazar()
        neutron = self.neutron()
        routers = {}

        reservable_ips = [
            ipaddress.IPv4Address(fip['floating_ip_address'])
            for fip in blazar.floatingip.list()
        ]

        with spinner():
            log('Retrieving all active Neutron ports')
            ports = neutron.list_ports().get("ports")

        public_ports = [
            p for p in ports
            if p["device_owner"] in [
                "network:floatingip",
                "network:router_gateway",
            ]
        ]
        ports_by_ip = {
            p.get("fixed_ips")[0].get("ip_address"): p
            for p in public_ports
        }

        with spinner():
            log('Retrieving all public allocation pools')
            allocation_pools = self._public_allocation_pools(neutron)

        all_addresses = list(reservable_ips)
        for p in allocation_pools:
            start_ip = ipaddress.IPv4Address(p.get("start"))
            end_ip = ipaddress.IPv4Address(p.get("end"))
            for net in ipaddress.summarize_address_range(start_ip, end_ip):
                all_addresses.extend(list(ipaddress.IPv4Network(net)))

        rows = []
        headers = [
            "public_ip",
            "allocation_type",
            "reservable",
            "project_id",
        ]

        with click.progressbar(sorted(all_addresses),
                               label='Processing addresses') as ips:
            for public_ip in ips:
                port = ports_by_ip.get(str(public_ip))
                reservable = str(public_ip in reservable_ips)

                allocation_type = "unallocated"
                project_id = "none"

                if port:
                    device_owner = port.get("device_owner")
                    if device_owner == "network:router_gateway":
                        allocation_type = "gateway"
                        router_id = port.get("device_id")
                        if router_id not in routers:
                            routers[router_id] = (
                                neutron.show_router(router_id).get("router"))
                        project_id = routers[router_id].get("project_id")
                    else:
                        allocation_type = "floating_ip"
                        project_id = port.get("project_id")

                rows.append([
                    str(public_ip),
                    allocation_type,
                    reservable,
                    project_id,
                ])

        click.echo(tabulate(rows, headers=headers))