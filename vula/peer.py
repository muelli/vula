from __future__ import annotations
from logging import Logger, getLogger
import time

import click
import pydbus
import yaml
import json

from io import StringIO
from datetime import timedelta

from .click import OrderedGroup

from ipaddress import (
    IPv4Address,
    IPv6Address,
    ip_address,
    ip_network,
)
from nacl.signing import SigningKey, VerifyKey
from nacl.exceptions import BadSignatureError

from base64 import b64encode
from schema import Schema, And, Or, Regex, Use, Optional
from typing import List

import codecs
from hkdf import Hkdf
from hashlib import sha512

from .constants import (
    _DATE_FMT,
    _LOG_FMT,
    _ORGANIZE_DBUS_NAME,
    _ORGANIZE_DBUS_PATH,
)

from .click import (
    DualUse,
    red,
    green,
    blue,
    yellow,
    bold,
    echo_maybepager,
    Exit,
)
from .engine import Result
from .common import (
    attrdict,
    DualUse,
    schemattrdict,
    serializable,
    b64_bytes,
    comma_separated_IPs,
    comma_separated_Nets,
    int_range,
    Flexibool,
    yamlrepr,
    yamlrepr_hl,
    queryable,
    schemadict,
    Length,
    organize_dbus_if_active,
    format_byte_stats,
)

_qrcode = None


@DualUse.object()
class Descriptor(schemattrdict, serializable):

    """
    Descriptors are the objects which are communicated between between publish
    and discover (via mDNS) and between discover and organize (via strings).

    This ridiculous test uses the fact that this is commandline-accessible:

    >>> x = Descriptor.cli.main(standalone_mode=False, args=tuple("--addrs 10.168.128.160 --c vdDpRSGtsqvui8dox0iBq0SSp/zXSEU2dx5s5x+qcquSp0oIWgDuqJw50e9wrIuGub+SXzU0s5EIRH49QmNYDw== --dt 86400 --e false --hostname wg-mdns-test3.local.  --pk EqcQ5gYxzGtzg7B4xi83kLyfuSMp8Kv3cmAJMs12nDM= --port 5354 --s T6htsKgwCp5MAXjPiWxtVkccg+K2CePsVa7uyUgxE2ouYKXg2qNL+0ut3sSbVTYjzFGZSCO6n80SRaR+BIeOCg== --vf 1606276812 --vk 90Y5JGEjoklSDw51ffoHYXhWs49TTnCQ/D5yBbuf3Zg= valid".split()))
    True

    For testing purposes, one could run the same command on the commandline like so:
        vula -d peer.Descriptor --addrs 10.168.128.160 --c vdDpRSGtsqvui8dox0iBq0SSp/zXSEU2dx5s5x+qcquSp0oIWgDuqJw50e9wrIuGub+SXzU0s5EIRH49QmNYDw== --dt 86400 --e false --hostname wg-mdns-test3.local.  --pk EqcQ5gYxzGtzg7B4xi83kLyfuSMp8Kv3cmAJMs12nDM= --port 5354 --r '' --s T6htsKgwCp5MAXjPiWxtVkccg+K2CePsVa7uyUgxE2ouYKXg2qNL+0ut3sSbVTYjzFGZSCO6n80SRaR+BIeOCg== --vf 1606276812 --vk 90Y5JGEjoklSDw51ffoHYXhWs49TTnCQ/D5yBbuf3Zg= valid

    ... but this is of course not the correct way to use this object.
    """

    schema = Schema(
        {
            'addrs': Use(
                comma_separated_IPs
            ),  # This is a list of endpoints by preference and is also used for AllowedIPs; fixme in the future
            'pk': b64_bytes.with_len(32),
            'c': b64_bytes.with_len(64),
            'hostname': And(
                str,
                # fixme: this should also ensure labels don't start or end with a dash, as per RFC1123
                Regex('^[a-zA-Z0-9.-]+$'),
                lambda name: 0 < len(name) < 256,
            ),
            'port': int_range(1, 65535),
            'vk': b64_bytes.with_len(32),
            'dt': Use(int),
            'vf': Use(int),
            'r': Use(
                comma_separated_Nets
            ),  # AllowedIPs may be set from this in the future; fixme
            'e': Flexibool,
            Optional('s'): b64_bytes.with_len(64),
        }
    )

    default = dict(r="")

    @classmethod
    def parse(cls, desc: str) -> Descriptor:
        """
        Parse the *descriptor* string line into a dictionary-like object. Carefully.

        This relies on the schema to coerce values into the right types.
        >>> addrs = "192.168.6.9"
        >>> c = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQQ=="
        >>> dt = "86400"
        >>> e = "0"
        >>> hostname = "alice.local"
        >>> pk = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="
        >>> port = "5354"
        >>> vf = "1601388653"
        >>> vk = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="
        >>> desc_s = f"addrs={addrs};c={c};dt={dt};e={e};hostname={hostname};pk={pk};port={port};vf={vf};vk={vk}"
        >>> d = Descriptor.parse(desc_s)
        >>> type(d)
        <class 'vula.peer.Descriptor'>
        >>> d['addrs']
        <comma_separated_IPs('192.168.6.9')>
        >>> d['c']
        <b64:QUFBQU...(64)>
        >>> d['dt']
        86400
        >>> d['e']
        0
        >>> d['hostname']
        'alice.local'
        >>> d['pk']
        <b64:QUFBQU...(32)>
        >>> d['port']
        5354
        >>> d['vf']
        1601388653
        >>> d['vk']
        <b64:QUFBQU...(32)>
        """
        try:
            split_desc: List = desc.split(";")
            dir_desc: dict = dict(
                val.split("=", maxsplit=1)
                for val in [kv.strip() for kv in split_desc]
                if len(val) > 1
            )
            descriptor: Descriptor = cls(dir_desc)
        except ValueError as error:
            raise
            # log.info("Unable to parse descriptor: %s (%r)", error, descriptor)
            return None
        return descriptor

    def _build_sig_buf(self: Descriptor) -> bytes:
        return " ".join(
            "%s=%s;" % (k, v) for k, v in sorted(self.items()) if k != 's'
        ).encode()

    
    def __str__(self):
        """
        Return the number or IP as a string

        >>> my_num = 12
        >>> my_num.__str__()
        '12'

        >>> ip = comma_separated_IPs('192.168.13.37')
        >>> ip.__str__()
        '192.168.13.37'
        """
        return " ".join("%s=%s;" % kv for kv in sorted(self.items()))

    @property
    def id(self):
        "This returns the peer ID (aka the verify key, base64-encoded)"
        return str(self.vk)

    @property
    def ephemeral(self):
        return self.e

    @property
    def valid(self):
        # XXX: placeholder until this is merged with other class with signature validation etc
        # ^^^
        return self.verify_signature()

    @property
    def IPv4addrs(self):
        return [ip for ip in self.addrs if isinstance(ip, IPv4Address)]

    @property
    def IPv6addrs(self):
        return [ip for ip in self.addrs if isinstance(ip, IPv6Address)]

    def sign(self, seed) -> Descriptor:
        signing_key: SigningKey = SigningKey(seed=seed)
        buf_to_sign: bytes = self._build_sig_buf()
        sig: bytes = signing_key.sign(buf_to_sign).signature
        descriptor = Descriptor(self, s=b64encode(sig).decode())
        return descriptor

    def verify_signature(self) -> bool:
        verify_public_key = VerifyKey(self.vk)
        sig = self.s
        buf_to_verify: bytes = self._build_sig_buf()
        try:
            verify_public_key.verify(buf_to_verify, sig)
            return True
        except BadSignatureError:
            return False

    def make_peer(self: Descriptor, **kwargs) -> Peer:
        peer = dict(
            descriptor=self,
            petname="",
            pinned=False,
            enabled=True,
            verified=False,
            nicknames={self.hostname: True},
            IPv4addrs={ip: True for ip in self.IPv4addrs},
            IPv6addrs={ip: True for ip in self.IPv6addrs},
            use_as_gateway=False,
        )
        peer.update(**kwargs)
        return Peer(peer)

    @property
    def qr_code(self):
        global _qrcode
        if _qrcode is None:
            import qrcode as _qrcode
        sio = StringIO()
        qr = _qrcode.QRCode()
        qr.add_data(data="local.vula:desc:" + str(self))
        qr.print_ascii(out=sio)
        sio.seek(0)
        return sio.read()


class Peer(schemattrdict):
    schema = Schema(
        And(
            {
                'descriptor': Use(Descriptor),
                'petname': str,
                'nicknames': {Optional(str): Flexibool},
                'IPv4addrs': {Optional(Use(IPv4Address)): Flexibool},
                'IPv6addrs': {Optional(Use(IPv6Address)): Flexibool},
                'enabled': Flexibool,
                'verified': Flexibool,
                'pinned': Flexibool,
                Optional('use_as_gateway'): Flexibool,
                Optional('_allow_unsigned_descriptor'): Flexibool,
            },
            #     lambda peer: peer['descriptor'].verify_signature() or peer.get('_allow_unsigned_descriptor')
        )
    )

    # Peers are currently only instantiated by Descriptor.make_peer, default is
    # there.
    default = None

    @property
    def id(self):
        "This returns the peer ID (aka the verify key, as a base64 string)"
        return self.descriptor.id

    @property
    def wg_pk(self):
        "WireGuard public key"
        return self.descriptor.pk

    @property
    def name(self):
        """
        This returns the best name, for display purposes.

        It is either the petname, or the last name announced, or, if the last
        name announced is disabled, another enabled name.
        >>> desc_string = f"addrs=192.168.6.9;c=QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQQ==;dt=86400;e=0;hostname=george.local;pk=QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI=;port=123;vf=1601388653;vk=Q0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0M="
        >>> descriptor = Descriptor.parse(desc_string)
        >>> Peer(dict(descriptor=descriptor,petname="george",pinned=False,enabled=True,verified=False,nicknames={descriptor.hostname: True},IPv4addrs={ip: True for ip in descriptor.IPv4addrs},IPv6addrs={ip: True for ip in descriptor.IPv6addrs})).name
        'george'
        >>> Peer(dict(descriptor=descriptor,petname="",pinned=False,enabled=True,verified=False,nicknames={descriptor.hostname: True},IPv4addrs={ip: True for ip in descriptor.IPv4addrs},IPv6addrs={ip: True for ip in descriptor.IPv6addrs})).name
        'george.local'
        >>> Peer(dict(descriptor=descriptor,petname="",pinned=False,enabled=True,verified=False,nicknames={descriptor.hostname: False, "schnubbi": True},IPv4addrs={ip: True for ip in descriptor.IPv4addrs},IPv6addrs={ip: True for ip in descriptor.IPv6addrs})).name
        'schnubbi'
        """
        latest = self.descriptor.hostname
        return self.get('petname') or (
            latest
            if self.nicknames[self.descriptor.hostname]
            else self.enabled_names[0]
            if self.enabled_names
            else "<unnamed>"
        )

    @property
    def other_names(self):
        return list(sorted(set(self.nicknames) - set([self.name])))

    @property
    def name_and_id(self):
        return "%s (%s)" % (self.name, self.id)

    @property
    def enabled_names(self):
        return sorted(
            set(
                ([self.petname] if self.petname else [])
                + [n for n in self.nicknames if self.nicknames[n]]
            )
        )

    @property
    def enabled_ips(self):
        return [ip for ip, on in self.IPv4addrs.items() if on] + [
            ip for ip, on in self.IPv6addrs.items() if on
        ]

    @property
    def disabled_ips(self):
        return [ip for ip, on in self.IPv4addrs.items() if not on] + [
            ip for ip, on in self.IPv6addrs.items() if not on
        ]

    @property
    def enabled_ips_str(self):
        return [str(ip) for ip in self.enabled_ips]

    @property
    def endpoint_addr(self):
        return self.descriptor.addrs[0]  # XXX use better "best ip" logic,
        # allowing for disabled IPs, etc

    @property
    def routes(self):
        res = [
            ip_network("%s/%s" % (ip, ip.max_prefixlen))
            for ip in self.enabled_ips
        ]
        return res

    @property
    def allowed_ips(self):
        res = [
            ip_network("%s/%s" % (ip, ip.max_prefixlen))
            for ip in self.enabled_ips
        ]
        if self.use_as_gateway:
            res.append(ip_network("0.0.0.0/0"))  # FIXME: ipv6
        return res

    @property
    def endpoint(self):
        return "%s:%s" % (self.endpoint_addr, self.descriptor.port)

    def wg_config(self: Peer, csidh_psk):
        return attrdict(
            public_key=str(self.descriptor.pk),
            endpoint_addr=str(self.endpoint_addr),
            endpoint_port=self.descriptor.port,
            allowed_ips=list(map(str, self.allowed_ips)),
            preshared_key=csidh_psk,
        )

    def show(self, stats=None):
        green_or_yellow = (
            green if self.pinned and self.verified and self.enabled else yellow
        )
        return "\n  ".join(
            bold(label) + ': ' + str(value) if label else str(value)
            for label, value in {
                green_or_yellow('peer'): green_or_yellow(self.name),
                'id': self.id,
                red('warning'): (
                    None if stats else red('wireguard peer is not configured')
                ),
                'other names': ", ".join(self.other_names),
                'status': " ".join(
                    filter(
                        None,
                        [
                            (red("disabled"), green("enabled"))[self.enabled],
                            (yellow("unpinned"), green("pinned"))[self.pinned],
                            (
                                (red if self.pinned else yellow)("unverified"),
                                green("verified"),
                            )[self.verified],
                            ('', bold(blue("gateway")))[self.use_as_gateway],
                        ],
                    )
                ),
                'endpoint': self.endpoint,
                'allowed ips': ", ".join(map(str, self.allowed_ips)),
                'disabled ips': ", ".join(map(str, self.disabled_ips)),
                'latest signature': (
                    str(
                        timedelta(
                            seconds=int(time.time() - self.descriptor.vf)
                        ),
                    )
                    + ' ago'
                ),
                'latest handshake': (
                    str(
                        timedelta(
                            seconds=int(
                                time.time() - stats['latest_handshake']
                            )
                        ),
                    )
                    + ' ago'
                    if stats and stats.get('latest_handshake')
                    else yellow('none')
                ),
                'transfer': (
                    stats
                    and sum(stats.values())
                    and "{rx_bytes} received, {tx_bytes} sent".format(
                        **format_byte_stats(stats)
                    )
                ),
                'wg pubkey': self.descriptor.pk,
            }.items()
            if value not in (None, False, '')
        )


class Peers(yamlrepr, queryable, schemadict):

    """
    A dictionry of peers. Note that, despite being the home of the conflict
    detection code, a Peers object can be valid (from a schema standpoint) even
    while containing conflicts. The schema of a SystemState object uses this
    conflict detection code to ensure that the peers object within a
    SystemState does not contain conflicts.
    """

    schema = Schema(
        {
            Optional(And(str, Length(44))): Use(Peer),
        },
    )

    def with_hostname(self: Peers, name: str):
        res = self.limit(enabled=True).by('nicknames').get(name, [])
        if len(res) > 1:
            raise Bug(
                # this should not be possible, as both the state logic and
                # schema prevent it
                "BUG: multiple hosts with the same name: %r"
                % (res,)
            )
        if len(res) < 1:
            raise KeyError("Hostname %r not found." % (name,))
        return res[0]

    def with_ip(self, ip):
        ip = IPv4Address(ip)
        res = self.limit(enabled=True).by('IPv4addrs').get(ip, [])
        if len(res) > 1:
            raise ConsistencyError(
                # this should also not be possible, because the state logic
                # prevents it (though the schema doesn't yet)
                "BUG: multiple hosts with the same IP: %r"
                % (res,)
            )
        if len(res) < 1:
            raise ValueError("No peer with IP %r" % (ip,))
        return res[0]

    @property
    def conflicts(self):
        "returns comma-separated list of colliding peer ids"
        enabled_gws = list(
            self.limit(use_as_gateway=True, enabled=True).values()
        )
        res = ",".join(
            [
                peer.id
                for peer in self.limit(enabled=True).values()
                if self.conflicts_for_descriptor(peer.descriptor)
            ]
            + [peer.id for peer in enabled_gws if len(enabled_gws) > 1]
        )
        return res

    def conflicts_for_descriptor(self, desc):
        """
        Returns list of enabled peers a descriptor has a conflicting wg_pk,
        hostname, or IP address with (ignoring itself).
        """
        return list(
            {
                conflict.id: conflict
                for conflict in self.limit(enabled=True)
                .by('enabled_names')
                .get(desc.hostname, [])
                + self.limit(enabled=True).by('wg_pk').get(desc.pk, [])
                + sum(
                    (
                        self.limit(enabled=True).by('IPv4addrs').get(ip, [])
                        for ip in desc.IPv4addrs
                    ),
                    [],
                )
                + sum(
                    (
                        self.limit(enabled=True).by('IPv6addrs').get(ip, [])
                        for ip in desc.IPv6addrs
                    ),
                    [],
                )
                if conflict.id != desc.id
            }.values()
        )

    def query(self, query):
        """
        Returns peer by vk, hostname, or IP. None if no match.
        """
        peer = (
            self.by('id').get(query)
            or self.limit(enabled=True).by('enabled_names').get(query)
            or self.limit(enabled=True).by('enabled_ips_str').get(query)
        )
        if peer:
            assert len(peer) == 1, ("this should not be possible:", peer)
            return peer[0]
        else:
            return None

def _ac_get_peer_ids(ctx, args, incomplete):
    organize = (
            ctx.meta.get('Organize', {}).get('magic_instance')
            or organize_dbus_if_active()
        )
    return organize.peer_ids('all')

@DualUse.object(
    short_help="View and modify peer information",
    invoke_without_command=True,
)
@click.pass_context
class PeerCommands(object):
    """
    Commands to view and modify peer information

    When "peer" is the top-level command, its subcommands communicate with
    organize via dbus. This is the normal way to use these commands (eg, you
    can run "vula peer show").

    At this point in development, "peer" may also be run as a subcommand of the
    "organize" command, in which case it will instantiate and operate on the
    organize object directly. In the case of the "set" and "remove" peer
    subcommands this only makes sense when there isn't an organize daemon
    process running (as the daemon process will overwrite the state file
    written by the "organize peer ..." command process without ever reading its
    contents). Note that we do not currently check to ensure that there is not
    an organize daemon running when "organize peer ..." commands are run.
    """

    def __init__(self, ctx):
        self.organize = (
            ctx.meta.get('Organize', {}).get('magic_instance')
            or organize_dbus_if_active()
        )

        if ctx.invoked_subcommand is None:
            self.show()

    @DualUse.method(short_help="Show peer information")
    @click.argument('peers', type=str, nargs=-1)
    @click.option(
        '-D',
        '--descriptor',
        is_flag=True,
        help="Print peer descriptor(s) instead of status",
    )
    @click.option('-q', '--qrcode', is_flag=True, help="Print ANSI qrcode(s)")
    @click.option(
        '-a',
        '--all',
        'which',
        flag_value='all',
        help="Show both enabled and disabled peers",
    )
    @click.option(
        '-d',
        '--disabled',
        'which',
        flag_value='disabled',
        help="Show only disabled peers",
    )
    @click.option(
        '-e',
        '--enabled',
        'which',
        flag_value='enabled',
        help="Show only enabled peers",
    )
    def show(self, peers=(), which=None, descriptor=False, qrcode=False):
        """
        Show peer information

        With no arguments, all enabled peers are shown.

        Peer arguments can be specified as ID, name, or IP, unless options are also
        specified in which case arguments must (currently) be IDs.
        """

        available = self.organize.peer_ids(which if which else 'enabled')

        queries = (
            available
            if peers == ()
            else [peer for peer in peers if peer in available]
            if which
            else peers
        )

        res = []
        for query in queries:
            if qrcode:
                d = self.organize.peer_descriptor(query)
                desc = Descriptor.parse(d)
                if descriptor:
                    res.append(d)
                else:
                    res.append("{vk} {hostname} {addrs}".format(**desc))
                res.append(desc.qr_code)
            elif descriptor:
                res.append(self.organize.peer_descriptor(query))
            else:
                res.append(self.organize.show_peer(query))

        echo_maybepager(("\n" if descriptor else "\n\n").join(res))

    @DualUse.method(short_help="Import peer descriptors", name='import')
    @click.argument('file', type=click.File(), default='-')
    def import_(self, file):
        """
        Import peer descriptors

        Reads from standard input if a file is not specified.

        Prints the result of processing each descriptor.

        Can consume the output of "vula peer show --descriptor" from another
        system, or the output of "vula discover --no-dbus --interface eth0".
        """
        while True:
            line = file.readline()
            line = line.split(':')[
                -1
            ].strip()  # strip timestamp from discover logs, if present
            if not line:
                break
            line = line.strip()
            res = self.organize.process_descriptor_string(line)
            if res:
                click.echo(yamlrepr_hl.from_yaml(res))
            else:
                click.echo("process_descriptor_string returned None 🤔")

    @DualUse.object(short_help="Add and remove peer addresses")
    @click.pass_context
    class addr(object):
        """
        Modify peer addresses

        In the future, this might also show addresses, but for now that can be
        done with the "peer show" command.
        """

        def __init__(self, ctx):
            self.organize = (
                ctx.meta.get('Organize', {}).get('magic_instance')
                or organize_dbus_if_active()
            )

        @DualUse.method()
        @click.argument('vk', type=str)
        @click.argument('ip', type=str)
        def add(self, vk, ip):
            """
            Add an address to a peer
            """
            click.echo(Result.from_yaml(self.organize.peer_addr_add(vk, ip)))

        @DualUse.method(name='del')
        @click.argument('vk', type=str)
        @click.argument('ip', type=str)
        def del_(self, vk, ip):
            """
            Delete an address from a peer

            Note: if the peer re-announces the address, it will be re-added. To
            prevent an address from being (re)enabled, you can set it to
            disabled instead of deleting it. This is currently accomplished
            using a command like "peer set $vk IPv4addrs x.x.x.x false"
            """
            click.echo(Result.from_yaml(self.organize.peer_addr_del(vk, ip)))

    @DualUse.method(short_help="Set arbitrary peer properties")
    @click.argument('vk', type=str, shell_complete=_ac_get_peer_ids)
    @click.argument('path', type=str, nargs=-1)
    @click.argument('value', type=str)
    def set(self, vk, path, value):
        """
        Modify arbitrary peer properties

        This is currently the only way to verify peers, enable/disable them,
        and enable or disable IP addresses.

        In the future, this command should perhaps only be available for
        debugging, and the normal user tasks which it currently performs should
        be handled by other commands.

        This command is *not* able to remove keys from dictionaries; for
        removing IP addresses (instead of disabling them with this command) you
        can use the "vula peer addr del" command. This command is able to add new
        IPs, but that is better done with "vula peer addr add".
        """
        res = Result(json.loads(self.organize.set_peer(vk, path, value)))
        click.echo(res)

    @DualUse.method()
    @click.argument('vk', type=str)
    def remove(self, vk):
        """
        Remove a peer
        """
        res = self.organize.remove_peer(vk)
        res = Result.from_yaml(str(res))
        return res


main = PeerCommands.cli
