import json
import celery
from faker import Faker
import datetime
from modularodm import Q
import requests

import mock  # noqa
from mock import call
from nose.tools import *  # noqa PEP8 asserts
import httpretty

from scripts import cleanup_failed_registrations as scripts

from framework.auth import Auth
from framework.tasks import handlers

from website.archiver import (
    ARCHIVER_CHECKING,
    ARCHIVER_PENDING,
    ARCHIVER_SUCCESS,
    ARCHIVER_FAILURE,
    ARCHIVE_COPY_FAIL,
    ARCHIVE_SIZE_EXCEEDED,
)
from website.archiver import utils as archiver_utils
from website.app import *  # noqa
from website import archiver
from website.archiver import listeners
from website.archiver.tasks import *   # noqa

from website import mails
from website import settings
from website.util import waterbutler_url_for
from website.project.model import Node
from website.addons.base import StorageAddonBase

from tests import factories
from tests.base import OsfTestCase

fake = Faker()

FILE_TREE = {
    'path': '/',
    'name': '',
    'kind': 'folder',
    'children': [
        {
            'path': '/1234567',
            'name': 'Afile.file',
            'kind': 'file',
            'size': '128',
        },
        {
            'path': '/qwerty',
            'name': 'A Folder',
            'kind': 'folder',
            'children': [
                {
                    'path': '/qwerty/asdfgh',
                    'name': 'coolphoto.png',
                    'kind': 'file',
                    'size': '256',
                }
            ],
        }
    ],
}

class ArchiverTestCase(OsfTestCase):
    def setUp(self):
        super(ArchiverTestCase, self).setUp()
        handlers.celery_before_request()
        self.user = factories.UserFactory()
        self.auth = Auth(user=self.user)
        self.src = factories.NodeFactory(creator=self.user)
        self.src.add_addon('dropbox', auth=self.auth)
        self.dst = factories.RegistrationFactory(user=self.user, project=self.src, send_signals=False)
        self.stat_result = aggregate_file_tree_metadata('dropbox', FILE_TREE, self.user)
        self.pks = (self.src._id, self.dst._id, self.user._id)

class TestStorageAddonBase(ArchiverTestCase):

    RESP_MAP = {
        '/': dict(data=FILE_TREE['children']),
        '/1234567': dict(data=FILE_TREE['children'][0]),
        '/qwerty': dict(data=FILE_TREE['children'][1]['children']),
        '/qwerty/asdfgh': dict(data=FILE_TREE['children'][1]['children'][0]),
    }

    @httpretty.activate
    def _test__get_file_tree(self, addon_short_name):
        requests_made = []
        def callback(request, uri, headers):
            path = request.querystring['path'][0]
            requests_made.append(path)
            return (200, headers, json.dumps(self.RESP_MAP[path]))

        for path in self.RESP_MAP.keys():
            url = waterbutler_url_for(
                'metadata',
                provider=addon_short_name,
                path=path,
                node=self.src,
                user=self.user,
                view_only=True,
            )
            httpretty.register_uri(httpretty.GET,
                                   url,
                                   body=callback,
                                   content_type='applcation/json')
        addon = self.src.get_or_add_addon(addon_short_name, auth=self.auth)
        root = {
            'path': '/',
            'name': '',
            'kind': 'folder',
        }
        file_tree = addon._get_file_tree(root, self.user)
        assert_equal(FILE_TREE, file_tree)
        assert_equal(requests_made, ['/', '/qwerty'])  # no requests made for files

    def _test_addon(self, addon_short_name):
        self._test__get_file_tree(addon_short_name)

    def test_addons(self):
        #  Test that each addon in settings.ADDONS_ARCHIVABLE implements the StorageAddonBase interface
        for addon in settings.ADDONS_ARCHIVABLE:
            self._test_addon(addon)

class TestArchiverTasks(ArchiverTestCase):

    @mock.patch('celery.chain')
    def test_archive(self, mock_chain):
        src_pk, dst_pk, user_pk = self.pks
        archive(src_pk, dst_pk, user_pk)
        stat_node_sig = stat_node.si(src_pk, dst_pk, user_pk)
        archive_node_sig = archive_node.s(src_pk, dst_pk, user_pk)
        mock_chain.assert_called_with(stat_node_sig, archive_node_sig)
        assert_true(self.dst.archiving)

    def test_stat_node(self):
        src_pk, dst_pk, user_pk = self.pks
        self.src.add_addon('box', auth=self.auth)  # has box, dropbox
        with mock.patch('celery.group') as mock_group:
            stat_node(src_pk, dst_pk, user_pk)
        stat_dropbox_sig = stat_addon.si('dropbox', src_pk, dst_pk, user_pk)
        stat_box_sig = stat_addon.si('box', src_pk, dst_pk, user_pk)
        assert(mock_group.called_with(stat_dropbox_sig, stat_box_sig))

    def test_stat_addon(self):
        src_pk, dst_pk, user_pk = self.pks
        with mock.patch.object(StorageAddonBase, '_get_file_tree') as mock_file_tree:
            with mock.patch.object(archiver, 'AggregateStatResult') as MockStat:
                mock_file_tree.return_value = FILE_TREE
                res = stat_addon('dropbox', src_pk,  dst_pk, user_pk)
        assert_equal(self.dst.archived_providers['dropbox']['status'], ARCHIVER_CHECKING)
        src_dropbox = self.src.get_addon('dropbox')
        assert(MockStat.called_with(
            src_dropbox._id,
            'dropbox',
            targets=[aggregate_file_tree_metadata(src_dropbox, FILE_TREE, self.user)]
        ))
        assert_equal(res.target_name, 'dropbox')

    def test_archive_node_pass(self):
        src_pk, dst_pk, user_pk = self.pks
        with mock.patch.object(StorageAddonBase, '_get_file_tree') as mock_file_tree:
            mock_file_tree.return_value = FILE_TREE
            result = stat_node.apply(args=(src_pk, dst_pk, user_pk)).result
        with mock.patch.object(celery, 'group') as mock_group:
            archive_node(result, src_pk, dst_pk, user_pk)
        archive_dropbox_signature = archive_addon.si(
            'dropbox',
            src_pk,
            dst_pk,
            user_pk,
            result
        )
        assert(mock_group.called_with(archive_dropbox_signature))

    def test_archive_node_fail(self):
        settings.MAX_ARCHIVE_SIZE = 100
        src_pk, dst_pk, user_pk = self.pks
        with mock.patch.object(StorageAddonBase, '_get_file_tree') as mock_file_tree:
            mock_file_tree.return_value = FILE_TREE
            result = stat_node.apply(args=(src_pk, dst_pk, user_pk)).result
        with mock.patch('website.archiver.utils.handle_archive_fail') as mock_fail:
            archive_node(result, src_pk, dst_pk, user_pk)
        mock_fail.assertcalled_with(
            ARCHIVE_SIZE_EXCEEDED,
            self.src,
            self.dst,
            self.user,
            result
        )

    def test_archive_addon(self):
        src_pk, dst_pk, user_pk = self.pks
        result = aggregate_file_tree_metadata('dropbox', FILE_TREE, self.user),
        with mock.patch.object(make_copy_request, 'si') as mock_make_copy_request:
            with mock.patch.object(requests, 'post'):
                archive_addon('dropbox', src_pk, dst_pk, user_pk, result)
        assert_equal(self.dst.archived_providers['dropbox']['status'], ARCHIVER_PENDING)
        cookie = self.user.get_or_create_cookie()
        assert(mock_make_copy_request.called_with(
            dst_pk,
            settings.WATERBUTLER_URL + '/ops/copy',
            data=dict(
                source=dict(
                    cookie=cookie,
                    nid=src_pk,
                    provider='dropbox',
                    path='/',
                ),
                destination=dict(
                    cookie=cookie,
                    nid=dst_pk,
                    provider=settings.ARCHIVE_PROVIDER,
                    path='/',
                ),
                rename='Archive of DropBox',
            )
        ))

    @httpretty.activate
    def test_make_copy_request_20X(self):
        src_pk, dst_pk, user_pk = self.pks
        def callback_OK(request, uri, headers):
            return (200, headers, json.dumps({}))

        self.dst.archived_providers = {
            'dropbox': {
                'status': ARCHIVER_PENDING
            }
        }
        self.dst.save()
        url = 'http://' + fake.ipv4()
        httpretty.register_uri(httpretty.POST,
                               url,
                               body=callback_OK,
                               content_type='application/json')
        with mock.patch.object(project_signals, 'archive_callback') as mock_callback:
            make_copy_request(dst_pk, url, {
                'source': {
                    'provider': 'dropbox'
                }
            })
        assert_equal(self.dst.archived_providers['dropbox']['status'], ARCHIVER_SUCCESS)
        assert(mock_callback.called_with(self.dst))

    @httpretty.activate
    def test_make_copy_request_error(self):
        error = {'errors': ['BAD REQUEST']}
        src_pk, dst_pk, user_pk = self.pks
        def callback_400(request, uri, headers):
            return (400, headers, json.dumps(error))

        self.dst.archived_providers = {
            'dropbox': {
                'status': ARCHIVER_PENDING
            }
        }
        self.dst.save()

        url = 'http://' + fake.ipv4()
        httpretty.register_uri(httpretty.POST,
                               url,
                               body=callback_400,
                               content_type='application/json')
        with mock.patch.object(archiver_utils, 'handle_archive_addon_error') as mock_catch_error:
            make_copy_request(dst_pk, url, {
                'source': {
                    'provider': 'dropbox'
                }
            })
        assert(mock_catch_error.called_with(self.dst, 'dropbox', error))

class TestArchiverUtils(ArchiverTestCase):

    @mock.patch('website.mails.send_mail')
    def test_handle_archive_fail(self, mock_send_mail):
        handle_archive_fail(
            ARCHIVE_COPY_FAIL,
            self.src,
            self.dst,
            self.user,
            {}
        )
        assert_equal(mock_send_mail.call_count, 2)
        assert_true(self.dst.is_deleted)

    @mock.patch('website.mails.send_mail')
    def test_handle_archive_fail_copy(self, mock_send_mail):
        handle_archive_fail(
            ARCHIVE_COPY_FAIL,
            self.src,
            self.dst,
            self.user,
            {}
        )
        args_user = dict(
            to_addr=self.user.username,
            user=self.user,
            src=self.src,
            mail=mails.ARCHIVE_COPY_ERROR_USER,
            results={},
            mimetype='html',
        )
        args_desk = dict(
            to_addr=settings.SUPPORT_EMAIL,
            user=self.user,
            src=self.src,
            mail=mails.ARCHIVE_COPY_ERROR_DESK,
            results={},
        )
        mock_send_mail.assert_has_calls([
            call(**args_user),
            call(**args_desk),
        ], any_order=True)

    @mock.patch('website.mails.send_mail')
    def test_handle_archive_fail_size(self, mock_send_mail):
        handle_archive_fail(
            ARCHIVE_SIZE_EXCEEDED,            
            self.src,
            self.dst,
            self.user,
            {}
        )
        args_user = dict(
            to_addr=self.user.username,
            user=self.user,
            src=self.src,
            mail=mails.ARCHIVE_SIZE_EXCEEDED_USER,
            stat_result={},
            mimetype='html',
        )
        args_desk = dict(
            to_addr=settings.SUPPORT_EMAIL,
            user=self.user,
            src=self.src,
            mail=mails.ARCHIVE_SIZE_EXCEEDED_DESK,
            stat_result={},
        )

        mock_send_mail.assert_has_calls([
            call(**args_user),
            call(**args_desk),
        ], any_order=True)

    def test_update_status(self):
        self.dst.archived_providers['test'] = {
            'status': 'OK',
        }
        self.dst.save()
        update_status(self.dst, 'test', 'BAD', meta={'meta': 'DATA'})
        assert_equal(self.dst.archived_providers['test']['status'], 'BAD')
        assert_equal(self.dst.archived_providers['test']['meta'], 'DATA')

    def test_aggregate_file_tree_metadata(self):
        a_stat_result = aggregate_file_tree_metadata('dropbox', FILE_TREE, self.user)
        assert_equal(a_stat_result.disk_usage, 128 + 256)
        assert_equal(a_stat_result.num_files, 2)
        assert_equal(len(a_stat_result.targets), 2)

    def test_archive_provider_for(self):
        provider = self.src.get_addon(settings.ARCHIVE_PROVIDER)
        assert_equal(archiver_utils.archive_provider_for(self.src, self.user)._id, provider._id)

    def test_has_archive_provider(self):
        assert_true(archiver_utils.has_archive_provider(self.src, self.user))
        wo = factories.NodeFactory(user=self.user)
        wo.delete_addon(settings.ARCHIVE_PROVIDER, auth=self.auth, _force=True)
        assert_false(archiver_utils.has_archive_provider(wo, self.user))

    def test_link_archive_provider(self):
        wo = factories.NodeFactory(user=self.user)
        wo.delete_addon(settings.ARCHIVE_PROVIDER, auth=self.auth, _force=True)
        archiver_utils.link_archive_provider(wo, self.user)
        assert_true(archiver_utils.has_archive_provider(wo, self.user))

    def test_cahandlerchive_addon_error(self):
        self.dst.archived_providers['dropbox'] = {
            'status': ARCHIVER_PENDING,
        }
        self.dst.save()

        errors = ['BAD REQUEST', 'BAD GATEWAY']
        archiver_utils.handle_archive_addon_error(self.dst, 'dropbox', errors)
        assert_equal(self.dst.archived_providers['dropbox']['status'], ARCHIVER_FAILURE)
        assert_equal(self.dst.archived_providers['dropbox']['errors'], errors)

    def test_delete_registration_tree(self):
        proj = factories.NodeFactory()
        factories.NodeFactory(parent=proj)
        comp2 = factories.NodeFactory(parent=proj)
        factories.NodeFactory(parent=comp2)
        reg = factories.RegistrationFactory(project=proj, send_signals=False)
        reg_ids = [reg._id] + [r._id for r in reg.get_descendants_recursive()]
        archiver_utils.delete_registration_tree(reg)
        assert_false(Node.find(Q('_id', 'in', reg_ids) & Q('is_deleted', 'eq', False)).count())


class TestArchiverListeners(ArchiverTestCase):

    def test_archive_node(self):
        with mock.patch.object(handlers, 'enqueue_task') as mock_queue:
            listeners.archive_node(self.src, self.dst, self.user)
        archive_signature = archive.si(self.src._id, self.dst._id, self.user._id)
        assert(mock_queue.called_with(archive_signature))

    def test_archive_node_links_unlinked(self):
        self.dst.delete_addon(settings.ARCHIVE_PROVIDER, auth=self.auth, _force=True)
        with mock.patch.object(handlers, 'enqueue_task') as mock_queue:
            listeners.archive_node(self.src, self.dst, self.user)
        archive_signature = archive.si(self.src._id, self.dst._id, self.user._id)
        assert(mock_queue.called_with(archive_signature))
        assert_true(archiver_utils.has_archive_provider(self.dst, self.user))

    def test_archive_callback_pending(self):
        self.dst.archived_providers = {
            addon: {
                'status': ARCHIVER_PENDING
            } for addon in settings.ADDONS_ARCHIVABLE
        }
        self.dst.archived_providers['osfstorage'] = {
            'status': ARCHIVER_SUCCESS
        }
        self.dst.save()
        with mock.patch('website.archiver.tasks.send_success_message') as mock_send:
            with mock.patch('website.archiver.utils.handle_archive_fail') as mock_fail:
                listeners.archive_callback(self.dst)
        assert_false(mock_send.called)
        assert_false(mock_fail.called)

    def test_archive_callback_done_success(self):
        self.dst.archived_providers = {
            addon: {
                'status': ARCHIVER_SUCCESS
            } for addon in settings.ADDONS_ARCHIVABLE
        }
        self.dst.save()
        with mock.patch.object(handlers, 'enqueue_task') as mock_enqueue:
            listeners.archive_callback(self.dst)
        send_success_message_sig = send_success_message.si(self.dst._id)
        assert(mock_enqueue.called_with(send_success_message_sig))

    def test_archive_callback_done_errors(self):
        self.dst.archived_providers = {
            addon: {
                'status': ARCHIVER_SUCCESS
            } for addon in settings.ADDONS_ARCHIVABLE
        }
        self.dst.archived_providers['osfstorage']['status'] = ARCHIVER_FAILURE
        self.dst.save()
        with mock.patch('website.archiver.utils.handle_archive_fail') as mock_fail:
            listeners.archive_callback(self.dst)
        assert(mock_fail.called_with(ARCHIVE_COPY_FAIL, self.src, self.dst, self.user, self.dst.archived_providers))


class TestArchiverScripts(ArchiverTestCase):

    def test_find_failed_registrations(self):
        failures = []
        delta = datetime.timedelta(2)
        for i in range(5):
            reg = factories.RegistrationFactory()
            reg._fields['registered_date'].__set__(
                reg,
                datetime.datetime.now() - delta,
                safe=True
            )
            reg.archived_providers = {
                addon: {
                    'status': ARCHIVER_PENDING
                } for addon in settings.ADDONS_ARCHIVABLE
            }
            reg.archiving = True
            reg.save()
            failures.append(reg)
        pending = []
        for i in range(5):
            reg = factories.RegistrationFactory()
            reg.archived_providers = {
                addon: {
                    'status': ARCHIVER_PENDING
                } for addon in settings.ADDONS_ARCHIVABLE
            }
            reg.archiving = True
            reg.save()
            pending.append(reg)
        failed = scripts.find_failed_registrations()
        assert_equal(failed.get_keys(), [f._id for f in failures])
