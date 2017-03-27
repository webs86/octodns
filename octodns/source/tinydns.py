#
#
#

from __future__ import absolute_import, division, print_function, \
    unicode_literals

from collections import defaultdict
from ipaddress import ip_address
from os import listdir
from os.path import join
import logging
import re

from ..record import Record
from ..zone import DuplicateRecordException, SubzoneRecordException
from .base import BaseSource


class TinyDnsBaseSource(BaseSource):
    SUPPORTS_GEO = False

    split_re = re.compile(r':+')

    def __init__(self, id, default_ttl=3600):
        super(TinyDnsBaseSource, self).__init__(id)
        self.default_ttl = default_ttl

    def _data_for_A(self, _type, records):
        values = []
        for record in records:
            if record[0] != '0.0.0.0':
                values.append(record[0])
        if len(values) == 0:
            return
        try:
            ttl = records[0][1]
        except IndexError:
            ttl = self.default_ttl
        return {
            'ttl': ttl,
            'type': _type,
            'values': values,
        }

    def _data_for_CNAME(self, _type, records):
        first = records[0]
        try:
            ttl = first[1]
        except IndexError:
            ttl = self.default_ttl
        return {
            'ttl': ttl,
            'type': _type,
            'value': '{}.'.format(first[0])
        }

    def _data_for_MX(self, _type, records):
        try:
            ttl = records[0][2]
        except IndexError:
            ttl = self.default_ttl
        return {
            'ttl': ttl,
            'type': _type,
            'values': [{
                'priority': r[1],
                'value': '{}.'.format(r[0])
            } for r in records]
        }

    def _data_for_NS(self, _type, records):
        try:
            ttl = records[0][1]
        except IndexError:
            ttl = self.default_ttl
        return {
            'ttl': ttl,
            'type': _type,
            'values': ['{}.'.format(r[0]) for r in records]
        }

    def populate(self, zone, target=False):
        self.log.debug('populate: zone=%s', zone.name)
        before = len(zone.records)

        if zone.name.endswith('in-addr.arpa.'):
            self._populate_in_addr_arpa(zone)
        else:
            self._populate_normal(zone)

        self.log.info('populate:   found %s records',
                      len(zone.records) - before)

    def _populate_normal(self, zone):
        type_map = {
            '=': 'A',
            '^': None,
            '.': 'NS',
            'C': 'CNAME',
            '+': 'A',
            '@': 'MX',
        }
        name_re = re.compile('((?P<name>.+)\.)?{}$'.format(zone.name[:-1]))

        data = defaultdict(lambda: defaultdict(list))
        for line in self._lines():
            _type = line[0]
            if _type not in type_map:
                # Something we don't care about
                continue
            _type = type_map[_type]
            if not _type:
                continue

            # Skip type, remove trailing comments, and omit newline
            line = line[1:].split('#', 1)[0]
            # Split on :'s including :: and strip leading/trailing ws
            line = [p.strip() for p in self.split_re.split(line)]
            match = name_re.match(line[0])
            if not match:
                continue
            name = zone.hostname_from_fqdn(line[0])
            data[name][_type].append(line[1:])

        for name, types in data.items():
            for _type, d in types.items():
                data_for = getattr(self, '_data_for_{}'.format(_type))
                data = data_for(_type, d)
                if data:
                    record = Record.new(zone, name, data, source=self)
                    try:
                        zone.add_record(record)
                    except SubzoneRecordException:
                        self.log.debug('_populate_normal: skipping subzone '
                                       'record=%s', record)

    def _populate_in_addr_arpa(self, zone):
        name_re = re.compile('(?P<name>.+)\.{}$'.format(zone.name[:-1]))

        for line in self._lines():
            _type = line[0]
            # We're only interested in = (A+PTR), and ^ (PTR) records
            if _type not in ('=', '^'):
                continue

            # Skip type, remove trailing comments, and omit newline
            line = line[1:].split('#', 1)[0]
            # Split on :'s including :: and strip leading/trailing ws
            line = [p.strip() for p in self.split_re.split(line)]

            if line[0].endswith('in-addr.arpa'):
                # since it's already in in-addr.arpa format
                match = name_re.match(line[0])
                value = '{}.'.format(line[1])
            else:
                addr = ip_address(line[1])
                match = name_re.match(addr.reverse_pointer)
                value = '{}.'.format(line[0])

            if match:
                try:
                    ttl = line[2]
                except IndexError:
                    ttl = self.default_ttl

                name = match.group('name')
                record = Record.new(zone, name, {
                    'ttl': ttl,
                    'type': 'PTR',
                    'value': value
                }, source=self)
                try:
                    zone.add_record(record)
                except DuplicateRecordException:
                    self.log.warn('Duplicate PTR record for {}, '
                                  'skipping'.format(addr))


class TinyDnsFileSource(TinyDnsBaseSource):
    '''
    A basic TinyDNS zonefile importer created to import legacy data.

    tinydns:
        class: octodns.source.tinydns.TinyDnsFileSource
        # The location of the TinyDNS zone files
        directory: ./zones
        # The ttl to use for records when not specified in the data
        # (optional, default 3600)
        default_ttl: 3600

    NOTE: timestamps & lo fields are ignored if present.
    '''
    def __init__(self, id, directory, default_ttl=3600):
        self.log = logging.getLogger('TinyDnsFileSource[{}]'.format(id))
        self.log.debug('__init__: id=%s, directory=%s, default_ttl=%d', id,
                       directory, default_ttl)
        super(TinyDnsFileSource, self).__init__(id, default_ttl)
        self.directory = directory
        self._cache = None

    def _lines(self):
        if self._cache is None:
            # We unfortunately don't know where to look since tinydns stuff can
            # be defined anywhere so we'll just read all files
            lines = []
            for filename in listdir(self.directory):
                if filename[0] == '.':
                    # Ignore hidden files
                    continue
                with open(join(self.directory, filename), 'r') as fh:
                    lines += filter(lambda l: l, fh.read().split('\n'))

            self._cache = lines

        return self._cache