#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright © 2010 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.

# Python
import datetime
import logging

# 3rd Party
import web

# Pulp
from pulp.server.api.cds import CdsApi
import pulp.server.api.cds_history as cds_history
from pulp.server.api.cds_history import CdsHistoryApi
from pulp.server.webservices import http
from pulp.server.webservices.controllers.base import JSONController
from pulp.server.webservices.role_check import RoleCheck


# globals ---------------------------------------------------------------------

cds_api = CdsApi()
cds_history_api = CdsHistoryApi()
log = logging.getLogger(__name__)

# restful controllers ---------------------------------------------------------

class CdsInstances(JSONController):

    @JSONController.error_handler
    @RoleCheck(admin=True)
    def GET(self):
        cds_instances = cds_api.list()
        return self.ok(cds_instances)

    @JSONController.error_handler
    @RoleCheck(admin=True)
    def PUT(self):
        repo_data = self.params()
        hostname = repo_data['hostname']

        existing = cds_api.cds(hostname)
        if existing is not None:
            return self.conflict('A CDS with the hostname [%s] already exists' % hostname)

        name = None
        description = None

        if 'name' in repo_data:
            name = repo_data['name']

        if 'description' in repo_data:
            description = repo_data['description']

        cds = cds_api.register(hostname, name, description)

        path = http.extend_uri_path(hostname)
        return self.created(path, cds)

class CdsInstance(JSONController):

    @JSONController.error_handler
    @RoleCheck(admin=True)
    def GET(self, id):
        cds = cds_api.cds(id)
        if cds is None:
            return self.not_found('Could not find CDS with hostname [%s]' % id)
        else:
            return self.ok(cds)

    @JSONController.error_handler
    @RoleCheck(admin=True)
    def DELETE(self, id):
        cds_api.unregister(id)
        return self.ok(True)

class CdsHistory(JSONController):

    @JSONController.error_handler
    @RoleCheck(admin=True)
    def POST(self, id):
        data = self.params()

        event_type = data.get('event_type', None)
        limit = data.get('limit', None)
        sort = data.get('sort', None)
        start_date = data.get('start_date', None)
        end_date = data.get('end_date', None)

        if sort is None:
            sort = cds_history.SORT_DESCENDING

        if limit:
            limit = int(limit)

        if start_date:
            start_date = datetime.datetime.strptime(start_date, '%Y-%m-%d')

        if end_date:
            end_date = datetime.datetime.strptime(end_date, '%Y-%m-%d')

        results = cds_history_api.query(cds_hostname=id, event_type=event_type, limit=limit,
                                        sort=sort, start_date=start_date, end_date=end_date)
        return self.ok(results)


# web.py application ----------------------------------------------------------

urls = (
    '/$', 'CdsInstances',
    '/history/([^/]+)/$', 'CdsHistory',
    '/([^/]+)/$', 'CdsInstance',
)

application = web.application(urls, globals())
