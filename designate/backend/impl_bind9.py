# Copyright 2012 Managed I.T.
#
# Author: Kiall Mac Innes <kiall@managedit.ie>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import os
import glob
import shutil
import time

from oslo.config import cfg

from designate.openstack.common import log as logging
from designate.openstack.common import lockutils
from designate.i18n import _LW
from designate import utils
from designate.backend import base


LOG = logging.getLogger(__name__)

cfg.CONF.register_group(cfg.OptGroup(
    name='backend:bind9', title="Configuration for BIND9 Backend"
))

cfg.CONF.register_opts([
    cfg.StrOpt('rndc-host', default='127.0.0.1', help='RNDC Host'),
    cfg.IntOpt('rndc-port', default=953, help='RNDC Port'),
    cfg.StrOpt('rndc-config-file', default=None,
               help='RNDC Config File'),
    cfg.StrOpt('rndc-key-file', default=None, help='RNDC Key File'),
    cfg.StrOpt('nzf-path', default='/var/cache/bind',
               help='Path where Bind9 stores the nzf files'),
], group='backend:bind9')


class Bind9Backend(base.Backend):
    __plugin_name__ = 'bind9'

    def start(self):
        super(Bind9Backend, self).start()

        domains = self.central_service.find_domains(self.admin_context)

        for domain in domains:
            rndc_op = 'reload'
            rndc_call = self._rndc_base() + [rndc_op]
            rndc_call.extend([domain['name']])

            try:
                LOG.debug('Calling RNDC with: %s' % " ".join(rndc_call))
                utils.execute(*rndc_call)
            except utils.processutils.ProcessExecutionError as proc_exec_err:
                stderr = proc_exec_err.stderr
                if stderr.count("rndc: 'reload' failed: not found") is not 0:
                    LOG.warn(_LW("Domain %(d_name)s (%(d_id)s) "
                                 "missing from backend, recreating") %
                             {'d_name': domain['name'], 'd_id': domain['id']})
                    self._sync_domain(domain, new_domain_flag=True)
                else:
                    raise proc_exec_err

    def create_domain(self, context, domain):
        LOG.debug('Create Domain')
        self._sync_domain(domain, new_domain_flag=True)

    def update_domain(self, context, domain):
        LOG.debug('Update Domain')
        self._sync_domain(domain)

    def delete_domain(self, context, domain):
        LOG.debug('Delete Domain')
        self._sync_delete_domain(domain)

    def create_recordset(self, context, domain, recordset):
        LOG.debug('Create RecordSet')
        self._sync_domain(domain)

    def update_recordset(self, context, domain, recordset):
        LOG.debug('Update RecordSet')
        self._sync_domain(domain)

    def delete_recordset(self, context, domain, recordset):
        LOG.debug('Delete RecordSet')
        self._sync_domain(domain)

    def create_record(self, context, domain, recordset, record):
        LOG.debug('Create Record')
        self._sync_domain(domain)

    def update_record(self, context, domain, recordset, record):
        LOG.debug('Update Record')
        self._sync_domain(domain)

    def delete_record(self, context, domain, recordset, record):
        LOG.debug('Delete Record')
        self._sync_domain(domain)

    def _rndc_base(self):
        rndc_call = [
            'rndc',
            '-s', cfg.CONF[self.name].rndc_host,
            '-p', str(cfg.CONF[self.name].rndc_port),
        ]

        if cfg.CONF[self.name].rndc_config_file:
            rndc_call.extend(['-c', cfg.CONF[self.name].rndc_config_file])

        if cfg.CONF[self.name].rndc_key_file:
            rndc_call.extend(['-k', cfg.CONF[self.name].rndc_key_file])

        return rndc_call

    def _sync_delete_domain(self, domain, new_domain_flag=False):
        """Remove domain zone files and reload bind config"""
        LOG.debug('Delete Domain: %s' % domain['id'])

        output_folder = os.path.join(os.path.abspath(cfg.CONF.state_path),
                                     'bind9')

        output_path = os.path.join(output_folder, '%s.zone' %
                                   "_".join([domain['name'], domain['id']]))

        os.remove(output_path)

        rndc_op = 'delzone'

        rndc_call = self._rndc_base() + [rndc_op, domain['name']]

        utils.execute(*rndc_call)

        # This goes and gets the name of the .nzf file that is a mirror of the
        # zones.config file we wish to maintain. The file name can change as it
        # is a hash of rndc view name, we're only interested in the first file
        # name this returns because there is only one .nzf file
        nzf_name = glob.glob('%s/*.nzf' % cfg.CONF[self.name].nzf_path)

        output_file = os.path.join(output_folder, 'zones.config')

        shutil.copyfile(nzf_name[0], output_file)

    def _sync_domain(self, domain, new_domain_flag=False):
        """Sync a single domain's zone file and reload bind config"""

        # NOTE: Only one thread should be working with the Zonefile at a given
        #       time. The sleep(1) below introduces a not insignificant risk
        #       of more than 1 thread working with a zonefile at a given time.
        with lockutils.lock('bind9-%s' % domain['id']):
            LOG.debug('Synchronising Domain: %s' % domain['id'])

            recordsets = self.central_service.find_recordsets(
                self.admin_context, {'domain_id': domain['id']})

            records = []

            for recordset in recordsets:
                criterion = {
                    'domain_id': domain['id'],
                    'recordset_id': recordset['id']
                }

                raw_records = self.central_service.find_records(
                    self.admin_context, criterion)

                for record in raw_records:
                    records.append({
                        'name': recordset['name'],
                        'type': recordset['type'],
                        'ttl': recordset['ttl'],
                        'priority': record['priority'],
                        'data': record['data'],
                    })

            output_folder = os.path.join(os.path.abspath(cfg.CONF.state_path),
                                         'bind9')

            output_name = "_".join([domain['name'], domain['id']])
            output_path = os.path.join(output_folder, '%s.zone' % output_name)

            utils.render_template_to_file('bind9-zone.jinja2',
                                          output_path,
                                          domain=domain,
                                          records=records)

            rndc_call = self._rndc_base()

            if new_domain_flag:
                rndc_op = [
                    'addzone',
                    '%s { type master; file "%s"; };' % (domain['name'],
                                                         output_path),
                ]
                rndc_call.extend(rndc_op)
            else:
                rndc_op = 'reload'
                rndc_call.extend([rndc_op])
                rndc_call.extend([domain['name']])

            if not new_domain_flag:
                # NOTE: Bind9 will only ever attempt to re-read a zonefile if
                #       the file's timestamp has changed since the previous
                #       reload. A one second sleep ensures we cross over a
                #       second boundary before allowing the next change.
                time.sleep(1)

            LOG.debug('Calling RNDC with: %s' % " ".join(rndc_call))
            utils.execute(*rndc_call)

            nzf_name = glob.glob('%s/*.nzf' % cfg.CONF[self.name].nzf_path)

            output_file = os.path.join(output_folder, 'zones.config')

            shutil.copyfile(nzf_name[0], output_file)
