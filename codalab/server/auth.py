'''
AuthHandler encapsulates the logic to authenticate users on the server-side.
'''
import base64
import json
import threading
import urllib
import urllib2


class User(object):
    '''
    Defines a registered user with a unique name and a unique (int) identifier.
    '''
    def __init__(self, name, unique_id):
        self.name = name
        self.unique_id = unique_id


class MockAuthHandler(object):
    '''
    A mock handler, which makes it easy to run a server when no real
    authentication is required.  There is exactly one root user with no
    password.
    '''
    def __init__(self, users):
        self.users = users
        self._user = users[0]

    def generate_token(self, grant_type, username, key):
        '''
        Always returns token information.
        '''
        matches = [user for user in self.users if user.name == username]
        if len(matches) == 0:
            return None
        self._user = matches[0]
        return {
            'token_type': 'Bearer',
            'access_token': '__mock_token__',
            'expires_in': 3600 * 24 * 365,
            'refresh_token': '__mock_token__',
        }

    def validate_token(self, token):
        '''
        Always returns True. The specified token is ignored.
        '''
        return True


    def get_users(self, key_type, keys):
        '''
        Resolves user names (key_type='names') or user IDs (key_type='ids') to
        corresponding User objects.

        key_type: The type of input keys: names or ids.
        keys: The set of names/ids to resolve.

        Returns a dictionary where keys are keys input to this method and
        values are either a User object or None if the key does not have
        a matching user (either the user does not exist or exists but is
        not active).
        '''
        def get_one(l): return l[0] if len(l) > 0 else None
        if key_type == 'names':
            return {key : get_one([user for user in self.users if key == user.name]) for key in keys}
        if key_type == 'ids':
            return {key : get_one([user for user in self.users if key == user.unique_id]) for key in keys}
        raise ValueError('Invalid key_type')

    def current_user(self):
        return self._user


class LocalUserFetcher(object):
    '''
    Base class for handlers that return users from the local database.
    '''
    def __init__(self, model):
        '''
        model: BundleModel instance
        '''
        self._model = model

    def get_users(self, key_type, keys):
        '''
        Resolves user names (key_type='names') or user IDs (key_type='ids') to
        corresponding User objects.

        key_type: The type of input keys: names or ids.
        keys: The set of names/ids to resolve.

        Returns a dictionary where keys are keys input to this method and
        values are either a User object or None if the key does not have
        a matching user (either the user does not exist or exists but is
        not active).
        '''
        # TODO(klopyrev): Once we've deprecated the OAuth handler that talks to
        # the Django server, we can migrate all code that uses this method to
        # the BundleModel version.
        user_ids = None
        usernames = None
        if key_type == 'ids':
            user_ids = keys
        elif key_type == 'names':
            usernames = keys
        else:
            raise ValueError('Invalid key_type')
        users = self._model.get_users(user_ids, usernames)
        user_dict = {}
        for user in users:
            key = user.user_id if key_type == 'ids' else user.user_name
            user_dict[key] = User(user.user_name, user.user_id)
        for key in keys:
            if key not in user_dict:
                user_dict[key] = None
        return user_dict


class RestOAuthHandler(threading.local, LocalUserFetcher):
    '''
    Handles user authentication with the REST bundle service server. Fetches
    other user records from the local database.

    Inherits from threading.local, which makes all instance attributes thread-local.
    When an OAuthHandler instance is used from a new thread, __init__ will be called
    again, and from thereon all attributes may be different between threads.
    https://hg.python.org/cpython/file/2.7/Lib/_threading_local.py
    '''
    def __init__(self, address, model):
        '''
        address: the address of the server
        model: BundleModel instance
        '''
        super(RestOAuthHandler, self).__init__(model)
        self._address = address
        self._user = None

    def generate_token(self, grant_type, username, key):
        '''
        Generate OAuth access token from username/password or from a refresh token.

        If the grant succeeds, the method returns a dictionary of the form:
        { 'token_type': 'Bearer',
          'access_token': <token>,
          'expires_in': <span in seconds>,
          'refresh_token': <token> }
        If the grant fails because of invalid credentials, None is returned.
        '''
        if grant_type == 'credentials':
            return self._make_token_request({
                'grant_type': 'password',
                'username': username,
                'password': key})
            return self._generate_token_from_credentials(username, key)
        if grant_type == 'refresh_token':
            return self._make_token_request({
                'grant_type': 'refresh_token',
                'refresh_token': key})
        raise ValueError("Bad request: grant_type is not valid.")

    def _make_token_request(self, data):
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Authorization': 'Basic ' + base64.b64encode('codalab_cli_client:'),
            'X-Requested-With': 'XMLHttpRequest'}
        request = urllib2.Request(
            self._address + '/rest/oauth2/token',
            headers=headers,
            data=urllib.urlencode(data))
        try:
            response = urllib2.urlopen(request)
            result = json.load(response)
            return result
        except urllib2.HTTPError as e:
            if e.code == 401:
                return None
            raise

    def validate_token(self, token):
        '''
        Validate OAuth authorization information.

        token: The token to validate. This value may be None to indicate that no
            Authorization header was specified. In such case this method will
            return true and set the current user to None.

        Returns True if the request is authorized to proceed. The current_user
            property of this class provides the user associated with the token.
        '''
        self._user = None
        if token is None:
            return True

        request = urllib2.Request(
            self._address + '/rest/oauth2/validate',
            headers={'Authorization': 'Bearer ' + token,
                     'X-Requested-With': 'XMLHttpRequest'})
        try:
            response = urllib2.urlopen(request)
            result = json.load(response)
            self._user = User(result['user_name'], result['user_id'])
            return True
        except urllib2.HTTPError as e:
            if e.code == 401:
                return False
            raise

    def current_user(self):
        '''
        Returns the current user as set by validate_token.
        '''
        return self._user


class LocalUserAuthHandler(LocalUserFetcher):
    '''
    Auth handler that takes the user during construction. Fetches other user
    records from the local database.
    '''
    def __init__(self, user, model):
        '''
        user: User to use, a codalab.object.user instance
        model: BundleModel instance
        '''
        super(LocalUserAuthHandler, self).__init__(model)
        if user is None:
            self._user = None
        else:
            self._user = User(user.user_name, user.user_id)

    def current_user(self):
        '''
        Returns the current user.
        '''
        return self._user
