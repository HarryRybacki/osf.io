# -*- coding: utf-8 -*-

import mock
from nose.tools import *  # noqa

from tests.base import OsfTestCase

import datetime

from website.addons.mendeley import model
from website.citations.models import CitationList


class MendeleyProviderTestCase(OsfTestCase):

    def setUp(self):
        super(MendeleyProviderTestCase, self).setUp()
        self.provider = model.Mendeley()

    @mock.patch('website.addons.mendeley.model.Mendeley._get_client')
    def test_handle_callback(self, mock_get_client):
        """Must return provider_id and display_name"""
        mock_client = mock.Mock()
        mock_client.profiles.me = mock.Mock(id='testid', display_name='testdisplay')
        mock_get_client.return_value = mock_client
        res = self.provider.handle_callback('testresponse')
        mock_get_client.assert_called_with('testresponse')
        assert_equal(res.get('provider_id'), 'testid')
        assert_equal(res.get('display_name'), 'testdisplay')

    @mock.patch('website.addons.mendeley.model.Mendeley._get_client')
    def test_client_not_cached(self, mock_get_client):
        """The first call to .client returns a new client"""
        mock_account = mock.Mock()
        mock_account.expires_at = datetime.datetime.now()
        self.provider.account = mock_account
        self.provider.client
        mock_get_client.assert_called
        assert_true(mock_get_client.called)

    @mock.patch('website.addons.mendeley.model.Mendeley._get_client')
    def test_client_cached(self, mock_get_client):
        """Repeated calls to .client returns the same client"""
        self.provider._client = mock.Mock()
        res = self.provider.client
        assert_equal(res, self.provider._client)
        assert_false(mock_get_client.called)

    @mock.patch('website.addons.mendeley.model.Mendeley._mendeley_folder_to_citation_list')
    def test_citation_lists(self, mock_folder_citations):
        """Get a list of Mendeley folders as CitationList instances

        Must also contain a CitationList to represent the account itself
        """
        mock_client = mock.Mock()
        mock_folders = [mock.Mock(), mock.Mock()]
        mock_list = mock.Mock()
        mock_list.items = mock_folders
        mock_client.folders.list.return_value = mock_list
        self.provider._client = mock_client
        mock_account = mock.Mock()
        self.provider.account = mock_account
        self.provider.citation_lists
        assert_equal(mock_folder_citations.call_args_list[0][0][0], mock_folders[0])
        assert_equal(mock_folder_citations.call_args_list[1][0][0], mock_folders[1])

    @mock.patch('website.addons.mendeley.model.Mendeley._citations_for_mendeley_user')
    @mock.patch('website.addons.mendeley.model.Mendeley._citations_for_mendeley_folder')
    def test_get_citation_list_folder(self, mock_folder_citations, mock_user_citations):
        """Get a single MendeleyList as a CitationList, inluding Citations"""
        mock_client = mock.Mock()
        mock_folder = mock.Mock()
        mock_folder.name = 'folder'
        mock_client.folders = {'folder': mock_folder}
        self.provider._client = mock_client
        mock_account = mock.Mock()
        self.provider.account = mock_account
        res = self.provider.get_list('folder')
        assert_true(isinstance(res, CitationList))
        assert_equal(res.name, 'folder')
        assert_equal(res.provider_account_id, mock_account.provider_id)
        assert_equal(res.provider_list_id, 'folder')
        res.citations
        mock_folder_citations.assert_called_with(mock_folder)
        assert_false(mock_user_citations.called)

    @mock.patch('website.addons.mendeley.model.Mendeley._citations_for_mendeley_user')
    @mock.patch('website.addons.mendeley.model.Mendeley._citations_for_mendeley_folder')
    def test_get_citation_list_no_folder(self, mock_folder_citations, mock_user_citations):
        mock_client = mock.Mock()
        mock_client.folders = {}
        self.provider._client = mock_client
        mock_account = mock.Mock()
        mock_account.display_name = 'name'
        self.provider.account = mock_account
        res = self.provider.get_list('folder')
        assert_true(isinstance(res, CitationList))
        assert_equal(res.name, 'name')
        assert_equal(res.provider_account_id, mock_account.provider_id)
        assert_equal(res.provider_list_id, 'folder')
        res.citations
        mock_user_citations.assert_called()
        assert_false(mock_folder_citations.called)


class MendeleyNodeSettingsTestCase(OsfTestCase):
    def test_api_not_cached(self):
        """The first call to .api returns a new object"""
        assert_true(False)

    def test_api_cached(self):
        """Repeated calls to .api returns the same object"""
        assert_true(False)

    def test_grant_oauth(self):
        """Grant the node access to a single folder in a Mendeley account"""
        assert_true(False)

    def test_revoke_oauth(self):
        """Revoke access to a Mendeley account for the node"""
        assert_true(False)

    def test_verify_oauth_current_user(self):
        """Confirm access to a Mendeley account attached to the current user"""
        assert_true(False)

    def test_verify_oauth_other_user(self):
        """Verify access to a Mendeley account's folder beloning to another user
        """
        assert_true(False)

    def test_verify_oauth_other_user_failed(self):
        """Verify access to a Mendeley account's folder where the account is
        associated with the node, but the folder is not
        """
        assert_true(False)

    def test_verify_json(self):
        """All values are passed to the node settings view"""
        assert_true(False)


class MendeleyUserSettingsTestCase(OsfTestCase):
    def test_get_connected_accounts(self):
        """Get all Mendeley accounts for user"""
        assert_true(False)

    def test_to_json(self):
        """All values are passed to the user settings view"""
        assert_true(False)
