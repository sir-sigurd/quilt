"""
API routes.
"""

from functools import wraps

import boto3
from flask import abort, redirect, render_template, request, url_for
from flask_json import as_json
from oauthlib.oauth2 import OAuth2Error
from requests_oauthlib import OAuth2Session

import requests

from . import app, db
from .models import Package, Version

OAUTH_BASE_URL = app.config['OAUTH']['base_url']
OAUTH_CLIENT_ID = app.config['OAUTH']['client_id']
OAUTH_CLIENT_SECRET = app.config['OAUTH']['client_secret']

ACCESS_TOKEN_URL = '/o/token/'
AUTHORIZE_URL = '/o/authorize/'


s3_client = boto3.client(
    's3',
    aws_access_key_id=app.config['AWS_ACCESS_KEY'],
    aws_secret_access_key=app.config['AWS_SECRET_KEY']
)


### Web routes ###

def _create_session():
    return OAuth2Session(
        client_id=OAUTH_CLIENT_ID,
        redirect_uri=url_for('oauth_callback', _external=True)
    )

@app.route('/login')
def login():
    session = _create_session()
    url, state = session.authorization_url(url=OAUTH_BASE_URL + AUTHORIZE_URL)

    return redirect(url)

@app.route('/oauth_callback')
def oauth_callback():
    # TODO: Check `state`? Do we need CSRF protection here?

    error = request.args.get('error')
    if error is not None:
        return render_template('oauth_fail.html', error=error)

    code = request.args.get('code')
    if code is None:
        abort(400)

    session = _create_session()
    try:
        resp = session.fetch_token(
            token_url=OAUTH_BASE_URL + ACCESS_TOKEN_URL,
            code=code,
            client_secret=OAUTH_CLIENT_SECRET
        )
        return render_template('oauth_success.html', code=resp['refresh_token'])
    except OAuth2Error as ex:
        return render_template('oauth_fail.html', error=ex.error)

@app.route('/api/token', methods=['POST'])
@as_json
def token():
    refresh_token = request.values.get('refresh_token')
    if refresh_token is None:
        abort(400)

    session = _create_session()

    try:
        resp = session.refresh_token(
            token_url=OAUTH_BASE_URL + ACCESS_TOKEN_URL,
            client_id=OAUTH_CLIENT_ID,  # Why??? The session object already has it!
            client_secret=OAUTH_CLIENT_SECRET,
            refresh_token=refresh_token
        )
    except OAuth2Error as ex:
        return dict(error=ex.error)

    return dict(
        refresh_token=resp['refresh_token'],
        access_token=resp['access_token'],
        expires_at=resp['expires_at']
    )


### API routes ###

def api(require_login=True):
    """
    Decorator for API requests.
    Handles auth and adds the username as the first argument.
    """
    def innerdec(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            auth = request.headers.get('Authorization')
            user = None

            if auth is None:
                if require_login:
                    abort(401)
            else:
                headers = {
                    'Authorization': auth
                }
                resp = requests.get(OAUTH_BASE_URL + '/api-root', headers=headers)
                if resp.status_code == requests.codes.ok:
                    data = resp.json()
                    user = data['current_user']
                elif resp.status_code == requests.codes.unauthorized:
                    abort(401)
                else:
                    abort(500)
            return f(user, *args, **kwargs)
        return wrapper
    return innerdec

@app.route('/qpm/datasets/<user>/<package_name>/', methods=['GET', 'PUT'])
@api()
@as_json
def dataset(auth_user, user, package_name):
    if auth_user != user:
        # TODO: Use the `Access` table.
        abort(403)

    if request.method == 'PUT':
        data = request.get_json()
        try:
            package_hash = data['hash']
        except (TypeError, KeyError):
            abort(400)
        if not isinstance(package_hash, str):
            abort(400)

        # Insert a package if it doesn't already exist.
        # TODO: Separate endpoint for just creating a package with no versions?
        package = (
            Package.query
            .with_for_update()
            .filter_by(owner=user, name=package_name)
            .one_or_none()
        )
        if package is None:
            package = Package(owner=user, name=package_name)
            db.session.add(package)

        version = Version(
            package=package,
            author=user,
            hash=package_hash,
            s3_bucket=app.config['PACKAGE_BUCKET_NAME'],
            s3_path='%s/%s.h5' % (user, package)
        )
        db.session.add(version)

        upload_url = s3_client.generate_presigned_url(
            'put_object',
            Params=dict(
                Bucket=version.s3_bucket,
                Key=version.s3_path
            ),
            ExpiresIn=600  # 10min
        )

        db.session.commit()

        return dict(
            upload_url=upload_url
        )
    else:
        version = (
            db.session.query(Version)
            .join(Version.package)
            .filter_by(owner=user, name=package_name)
            .order_by(Version.id.desc())
            .first()
        )

        if version is None:
            abort(404)

        url = s3_client.generate_presigned_url(
            'get_object',
            Params=dict(
                Bucket=version.s3_bucket,
                Key=version.s3_path
            ),
            ExpiresIn=600  # 10min
        )

        return dict(
            url=url,
            hash=version.hash
        )
