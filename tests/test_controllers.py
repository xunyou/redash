from contextlib import contextmanager
import json
import time
from unittest import TestCase
from flask import url_for
from flask.ext.login import current_user
from mock import patch
from tests import BaseTestCase
from tests.factories import dashboard_factory, widget_factory, visualization_factory, query_factory, \
    query_result_factory, user_factory, data_source_factory
from redash import models, settings
from redash.wsgi import app
from redash.utils import json_dumps
from redash.authentication import sign


settings.GOOGLE_APPS_DOMAIN = "example.com"

@contextmanager
def authenticated_user(c, user=None):
    if not user:
        user = user_factory.create()

    with c.session_transaction() as sess:
        sess['user_id'] = user.id

    yield


def json_request(method, path, data=None):
    if data:
        response = method(path, data=json_dumps(data))
    else:
        response = method(path)

    if response.data:
        response.json = json.loads(response.data)
    else:
        response.json = None

    return response


class AuthenticationTestMixin():
    def test_redirects_when_not_authenticated(self):
        with app.test_client() as c:
            for path in self.paths:
                rv = c.get(path)
                self.assertEquals(302, rv.status_code)

    def test_returns_content_when_authenticated(self):
        with app.test_client() as c, authenticated_user(c):
            for path in self.paths:
                rv = c.get(path)
                self.assertEquals(200, rv.status_code)


class TestAuthentication(BaseTestCase):
    def test_redirects_for_nonsigned_in_user(self):
        with app.test_client() as c:
            rv = c.get("/")
            self.assertEquals(302, rv.status_code)


class PingTest(TestCase):
    def test_ping(self):
        with app.test_client() as c:
            rv = c.get('/ping')
            self.assertEquals(200, rv.status_code)
            self.assertEquals('PONG.', rv.data)


class IndexTest(BaseTestCase, AuthenticationTestMixin):
    def setUp(self):
        self.paths = ['/', '/dashboard/example', '/queries/1', '/admin/status']
        super(IndexTest, self).setUp()


class StatusTest(BaseTestCase):
    def test_returns_data_for_admin(self):
        admin = user_factory.create(groups=['admin', 'default'])
        with app.test_client() as c, authenticated_user(c, user=admin):
            rv = c.get('/status.json')
            self.assertEqual(rv.status_code, 200)

    def test_returns_403_for_non_admin(self):
        with app.test_client() as c, authenticated_user(c):
            rv = c.get('/status.json')
            self.assertEqual(rv.status_code, 403)

    def test_redirects_non_authenticated_user(self):
        with app.test_client() as c:
            rv = c.get('/status.json')
            self.assertEqual(rv.status_code, 302)


class DashboardAPITest(BaseTestCase, AuthenticationTestMixin):
    def setUp(self):
        self.paths = ['/api/dashboards']
        super(DashboardAPITest, self).setUp()

    def test_get_dashboard(self):
        d1 = dashboard_factory.create()
        with app.test_client() as c, authenticated_user(c):
            rv = c.get('/api/dashboards/{0}'.format(d1.slug))
            self.assertEquals(rv.status_code, 200)
            self.assertDictEqual(json.loads(rv.data), d1.to_dict(with_widgets=True))

    def test_get_non_existint_dashbaord(self):
        with app.test_client() as c, authenticated_user(c):
            rv = c.get('/api/dashboards/not_existing')
            self.assertEquals(rv.status_code, 404)

    def test_create_new_dashboard(self):
        user = user_factory.create()
        with app.test_client() as c, authenticated_user(c, user=user):
            dashboard_name = 'Test Dashboard'
            rv = json_request(c.post, '/api/dashboards', data={'name': dashboard_name})
            self.assertEquals(rv.status_code, 200)
            self.assertEquals(rv.json['name'], 'Test Dashboard')
            self.assertEquals(rv.json['user_id'], user.id)
            self.assertEquals(rv.json['layout'], [])

    def test_update_dashboard(self):
        d = dashboard_factory.create()
        new_name = 'New Name'
        with app.test_client() as c, authenticated_user(c):
            rv = json_request(c.post, '/api/dashboards/{0}'.format(d.id),
                              data={'name': new_name, 'layout': '[]'})
            self.assertEquals(rv.status_code, 200)
            self.assertEquals(rv.json['name'], new_name)

    def test_delete_dashboard(self):
        d = dashboard_factory.create()
        with app.test_client() as c, authenticated_user(c):
            rv = json_request(c.delete, '/api/dashboards/{0}'.format(d.slug))
            self.assertEquals(rv.status_code, 200)

            d = models.Dashboard.get_by_slug(d.slug)
            self.assertTrue(d.is_archived)


class WidgetAPITest(BaseTestCase):
    def create_widget(self, dashboard, visualization, width=1):
        data = {
            'visualization_id': visualization.id,
            'dashboard_id': dashboard.id,
            'options': {},
            'width': width
        }

        with app.test_client() as c, authenticated_user(c):
            rv = json_request(c.post, '/api/widgets', data=data)

        return rv

    def test_create_widget(self):
        dashboard = dashboard_factory.create()
        vis = visualization_factory.create()

        rv = self.create_widget(dashboard, vis)
        self.assertEquals(rv.status_code, 200)

        dashboard = models.Dashboard.get(models.Dashboard.id == dashboard.id)
        self.assertEquals(unicode(rv.json['layout']), dashboard.layout)

        self.assertEquals(dashboard.widgets, 1)
        self.assertEquals(rv.json['layout'], [[rv.json['widget']['id']]])
        self.assertEquals(rv.json['new_row'], True)

        rv2 = self.create_widget(dashboard, vis)
        self.assertEquals(dashboard.widgets, 2)
        self.assertEquals(rv2.json['layout'],
                          [[rv.json['widget']['id'], rv2.json['widget']['id']]])
        self.assertEquals(rv2.json['new_row'], False)

        rv3 = self.create_widget(dashboard, vis)
        self.assertEquals(rv3.json['new_row'], True)
        rv4 = self.create_widget(dashboard, vis, width=2)
        self.assertEquals(rv4.json['layout'],
                          [[rv.json['widget']['id'], rv2.json['widget']['id']],
                           [rv3.json['widget']['id']],
                           [rv4.json['widget']['id']]])
        self.assertEquals(rv4.json['new_row'], True)

    def test_create_text_widget(self):
        dashboard = dashboard_factory.create()

        data = {
            'visualization_id': None,
            'text': 'Sample text.',
            'dashboard_id': dashboard.id,
            'options': {},
            'width': 2
        }

        with app.test_client() as c, authenticated_user(c):
            rv = json_request(c.post, '/api/widgets', data=data)

        self.assertEquals(rv.status_code, 200)
        self.assertEquals(rv.json['widget']['text'], 'Sample text.')

    def test_delete_widget(self):
        widget = widget_factory.create()

        with app.test_client() as c, authenticated_user(c):
            rv = json_request(c.delete, '/api/widgets/{0}'.format(widget.id))

            self.assertEquals(rv.status_code, 200)
            dashboard = models.Dashboard.get_by_slug(widget.dashboard.slug)
            self.assertEquals(dashboard.widgets.count(), 0)
            self.assertEquals(dashboard.layout, '[]')

            # TODO: test how it updates the layout


class QueryAPITest(BaseTestCase, AuthenticationTestMixin):
    def setUp(self):
        self.paths = ['/api/queries']
        super(QueryAPITest, self).setUp()

    def test_update_query(self):
        query = query_factory.create()

        with app.test_client() as c, authenticated_user(c):
            rv = json_request(c.post, '/api/queries/{0}'.format(query.id), data={'name': 'Testing'})
            self.assertEqual(rv.status_code, 200)
            self.assertEquals(rv.json['name'], 'Testing')

    def test_create_query(self):
        user = user_factory.create()
        data_source = data_source_factory.create()
        query_data = {
            'name': 'Testing',
            'query': 'SELECT 1',
            'ttl': 3600,
            'data_source_id': data_source.id
        }

        with app.test_client() as c, authenticated_user(c, user=user):
            rv = json_request(c.post, '/api/queries', data=query_data)

            self.assertEquals(rv.status_code, 200)
            self.assertDictContainsSubset(query_data, rv.json)
            self.assertEquals(rv.json['user']['id'], user.id)
            self.assertIsNotNone(rv.json['api_key'])
            self.assertIsNotNone(rv.json['query_hash'])

            query = models.Query.get_by_id(rv.json['id'])
            self.assertEquals(len(list(query.visualizations)), 1)

    def test_get_query(self):
        query = query_factory.create()

        with app.test_client() as c, authenticated_user(c):
            rv = json_request(c.get, '/api/queries/{0}'.format(query.id))

            self.assertEquals(rv.status_code, 200)
            d = query.to_dict(with_visualizations=True)
            d.pop('created_at')
            self.assertDictContainsSubset(d, rv.json)

    def test_get_all_queries(self):
        queries = [query_factory.create() for _ in range(10)]

        with app.test_client() as c, authenticated_user(c):
            rv = json_request(c.get, '/api/queries')

            self.assertEquals(rv.status_code, 200)
            self.assertEquals(len(rv.json), 10)


class VisualizationAPITest(BaseTestCase):
    def test_create_visualization(self):
        query = query_factory.create()
        data = {
            'query_id': query.id,
            'name': 'Chart',
            'description':'',
            'options': {},
            'type': 'CHART'
        }

        with app.test_client() as c, authenticated_user(c):
            rv = json_request(c.post, '/api/visualizations', data=data)

            self.assertEquals(rv.status_code, 200)
            data.pop('query_id')
            self.assertDictContainsSubset(data, rv.json)

    def test_delete_visualization(self):
        visualization = visualization_factory.create()
        with app.test_client() as c, authenticated_user(c):
            rv = json_request(c.delete, '/api/visualizations/{0}'.format(visualization.id))

            self.assertEquals(rv.status_code, 200)
            self.assertEquals(models.Visualization.select().count(), 0)

    def test_update_visualization(self):
        visualization = visualization_factory.create()

        with app.test_client() as c, authenticated_user(c):
            rv = json_request(c.post, '/api/visualizations/{0}'.format(visualization.id),
                              data={'name': 'After Update'})

            self.assertEquals(rv.status_code, 200)
            self.assertEquals(rv.json['name'], 'After Update')


class QueryResultAPITest(BaseTestCase, AuthenticationTestMixin):
    def setUp(self):
        self.paths = []
        super(QueryResultAPITest, self).setUp()


class JobAPITest(BaseTestCase, AuthenticationTestMixin):
    def setUp(self):
        self.paths = []
        super(JobAPITest, self).setUp()


class CsvQueryResultAPITest(BaseTestCase, AuthenticationTestMixin):
    def setUp(self):
        super(CsvQueryResultAPITest, self).setUp()

        self.paths = []
        self.query_result = query_result_factory.create()
        self.query = query_factory.create()
        self.path = '/api/queries/{0}/results/{1}.csv'.format(self.query.id, self.query_result.id)

    # TODO: factor out the HMAC authentication tests

    def signature(self, expires):
        return sign(self.query.api_key, self.path, expires)

    def test_redirect_when_unauthenticated(self):
        with app.test_client() as c:
            rv = c.get(self.path)
            self.assertEquals(rv.status_code, 302)

    def test_redirect_for_wrong_signature(self):
        with app.test_client() as c:
            rv = c.get('/api/queries/{0}/results/{1}.csv'.format(self.query.id, self.query_result.id), query_string={'signature': 'whatever', 'expires': 0})
            self.assertEquals(rv.status_code, 302)

    def test_redirect_for_correct_signature_and_wrong_expires(self):
        with app.test_client() as c:
            rv = c.get('/api/queries/{0}/results/{1}.csv'.format(self.query.id, self.query_result.id), query_string={'signature': self.signature(0), 'expires': 0})
            self.assertEquals(rv.status_code, 302)

    def test_redirect_for_correct_signature_and_no_expires(self):
        with app.test_client() as c:
            rv = c.get('/api/queries/{0}/results/{1}.csv'.format(self.query.id, self.query_result.id), query_string={'signature': self.signature(time.time()+3600)})
            self.assertEquals(rv.status_code, 302)

    def test_redirect_for_correct_signature_and_expires_too_long(self):
        with app.test_client() as c:
            expires = time.time()+(10*3600)
            rv = c.get('/api/queries/{0}/results/{1}.csv'.format(self.query.id, self.query_result.id), query_string={'signature': self.signature(expires), 'expires': expires})
            self.assertEquals(rv.status_code, 302)

    def test_returns_200_for_correct_signature(self):
        with app.test_client() as c:
            expires = time.time()+1800
            rv = c.get('/api/queries/{0}/results/{1}.csv'.format(self.query.id, self.query_result.id), query_string={'signature': self.signature(expires), 'expires': expires})
            self.assertEquals(rv.status_code, 200)

    def test_returns_200_for_authenticated_user(self):
        with app.test_client() as c, authenticated_user(c):
            rv = c.get('/api/queries/{0}/results/{1}.csv'.format(self.query.id, self.query_result.id))
            self.assertEquals(rv.status_code, 200)


class TestLogin(BaseTestCase):
    def setUp(self):
        settings.PASSWORD_LOGIN_ENABLED = True
        super(TestLogin, self).setUp()

    def test_redirects_to_google_login_if_password_disabled(self):
        with app.test_client() as c, patch.object(settings, 'PASSWORD_LOGIN_ENABLED', False):
            rv = c.get('/login')
            self.assertEquals(rv.status_code, 302)
            self.assertTrue(rv.location.endswith(url_for('google_oauth.authorize')))

    def test_get_login_form(self):
        with app.test_client() as c:
            rv = c.get('/login')
            self.assertEquals(rv.status_code, 200)

    def test_submit_non_existing_user(self):
        with app.test_client() as c, patch('redash.controllers.login_user') as login_user_mock:
            rv = c.post('/login', data={'username': 'arik', 'password': 'password'})
            self.assertEquals(rv.status_code, 200)
            self.assertFalse(login_user_mock.called)

    def test_submit_correct_user_and_password(self):

        user = user_factory.create()
        user.hash_password('password')
        user.save()

        with app.test_client() as c, patch('redash.controllers.login_user') as login_user_mock:
            rv = c.post('/login', data={'username': user.email, 'password': 'password'})
            self.assertEquals(rv.status_code, 302)
            login_user_mock.assert_called_with(user, remember=False)

    def test_submit_correct_user_and_password_and_remember_me(self):
        user = user_factory.create()
        user.hash_password('password')
        user.save()

        with app.test_client() as c, patch('redash.controllers.login_user') as login_user_mock:
            rv = c.post('/login', data={'username': user.email, 'password': 'password', 'remember': True})
            self.assertEquals(rv.status_code, 302)
            login_user_mock.assert_called_with(user, remember=True)

    def test_submit_correct_user_and_password_with_next(self):
        user = user_factory.create()
        user.hash_password('password')
        user.save()

        with app.test_client() as c, patch('redash.controllers.login_user') as login_user_mock:
            rv = c.post('/login?next=/test',
                        data={'username': user.email, 'password': 'password'})
            self.assertEquals(rv.status_code, 302)
            self.assertEquals(rv.location, 'http://localhost/test')
            login_user_mock.assert_called_with(user, remember=False)

    def test_submit_incorrect_user(self):
        with app.test_client() as c, patch('redash.controllers.login_user') as login_user_mock:
            rv = c.post('/login', data={'username': 'non-existing', 'password': 'password'})
            self.assertEquals(rv.status_code, 200)
            self.assertFalse(login_user_mock.called)

    def test_submit_incorrect_password(self):
        user = user_factory.create()
        user.hash_password('password')
        user.save()

        with app.test_client() as c, patch('redash.controllers.login_user') as login_user_mock:
            rv = c.post('/login', data={'username': user.email, 'password': 'badbadpassword'})
            self.assertEquals(rv.status_code, 200)
            self.assertFalse(login_user_mock.called)

    def test_submit_incorrect_password(self):
        user = user_factory.create()

        with app.test_client() as c, patch('redash.controllers.login_user') as login_user_mock:
            rv = c.post('/login', data={'username': user.email, 'password': ''})
            self.assertEquals(rv.status_code, 200)
            self.assertFalse(login_user_mock.called)

    def test_user_already_loggedin(self):
        with app.test_client() as c, authenticated_user(c), patch('redash.controllers.login_user') as login_user_mock:
            rv = c.get('/login')
            self.assertEquals(rv.status_code, 302)
            self.assertFalse(login_user_mock.called)

    # TODO: brute force protection?


class TestLogout(BaseTestCase):
    def test_logout_when_not_loggedin(self):
        with app.test_client() as c:
            rv = c.get('/logout')
            self.assertEquals(rv.status_code, 302)
            self.assertFalse(current_user.is_authenticated())

    def test_logout_when_loggedin(self):
        with app.test_client() as c, authenticated_user(c):
            rv = c.get('/')
            self.assertTrue(current_user.is_authenticated())
            rv = c.get('/logout')
            self.assertEquals(rv.status_code, 302)
            self.assertFalse(current_user.is_authenticated())